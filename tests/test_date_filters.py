"""Date and exclusion filters.

Both exist because an agent hand-rolling SQL got a confident wrong answer. Index timestamps
are MILLISECONDS, and `datetime(updated_at, 'unixepoch')` on a millisecond value returns NULL
instead of erroring, so a date-bounded query answers "no sessions" with nothing to show
anything went wrong. The CLI now owns the arithmetic.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from session_buddy.database import IndexDatabase
from session_buddy.models import SessionRecord
from session_buddy.search import parse_day, search_sessions


def at(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def seed(tmp_path, rows: list[tuple[str, str]]):
    db = IndexDatabase(tmp_path / "index.sqlite")
    try:
        for session_id, day in rows:
            db.upsert_session(
                SessionRecord(
                    provider="claude", session_id=session_id, title=f"session {session_id}",
                    cwd="/repo", created_at=at(day), updated_at=at(day), git_branch="main",
                    source_path=f"/tmp/{session_id}", file_mtime=1, file_size=1,
                    message_count=1, first_prompt="webhook retry", summary="",
                    preview="webhook retry",
                ),
                embedder=None,
            )
        db.conn.commit()
        return db.path
    finally:
        db.close()


def test_parse_day_returns_milliseconds():
    """The unit the index stores. Seconds here would silently match nothing."""
    value = parse_day("2026-10-18")

    # Round-trip rather than restate the arithmetic: a second copy of the formula would
    # agree with a wrong formula.
    assert datetime.fromtimestamp(value / 1000, timezone.utc).date().isoformat() == "2026-10-18"
    assert value > 1_000_000_000_000, "milliseconds, not seconds"


def test_a_millisecond_timestamp_breaks_the_obvious_sql(tmp_path):
    """Pins the trap itself: the naive filter returns no rows and no error."""
    path = seed(tmp_path, [("a", "2026-10-18")])
    db = IndexDatabase(path)
    try:
        naive = db.conn.execute(
            "select count(*) from sessions where datetime(updated_at, 'unixepoch') > '2026-01-01'"
        ).fetchone()[0]
        correct = db.conn.execute(
            "select count(*) from sessions where datetime(updated_at / 1000, 'unixepoch') > '2026-01-01'"
        ).fetchone()[0]
    finally:
        db.close()

    assert naive == 0, "the naive filter must be shown to answer zero"
    assert correct == 1


def test_since_keeps_only_later_sessions(tmp_path):
    path = seed(tmp_path, [("old", "2026-06-01"), ("new", "2026-10-18")])

    hits = search_sessions(path, "webhook", semantic=False, since="2026-10-01")

    assert [h.session_id for h in hits] == ["new"]


def test_until_includes_the_named_day(tmp_path):
    """An exclusive bound would drop the day someone asked for."""
    path = seed(tmp_path, [("same", "2026-06-01"), ("later", "2026-06-02")])

    hits = search_sessions(path, "webhook", semantic=False, until="2026-06-01")

    assert [h.session_id for h in hits] == ["same"]


def test_a_single_day_window_is_possible(tmp_path):
    path = seed(tmp_path, [("before", "2026-05-31"), ("day", "2026-06-01"), ("after", "2026-06-02")])

    hits = search_sessions(path, "webhook", semantic=False, since="2026-06-01", until="2026-06-01")

    assert [h.session_id for h in hits] == ["day"]


def test_exclude_drops_the_named_session(tmp_path):
    """An agent searching its own topic ranks its own transcript first."""
    path = seed(tmp_path, [("mine", "2026-10-18"), ("theirs", "2026-10-17")])

    before = {h.session_id for h in search_sessions(path, "webhook", semantic=False)}
    after = {h.session_id for h in search_sessions(path, "webhook", semantic=False, exclude=["mine"])}

    assert "mine" in before, "the fixture must produce the row the filter removes"
    assert after == {"theirs"}


def test_a_bad_day_is_rejected_rather_than_ignored(tmp_path):
    path = seed(tmp_path, [("a", "2026-10-18")])

    with pytest.raises(ValueError):
        search_sessions(path, "webhook", semantic=False, since="last tuesday")
