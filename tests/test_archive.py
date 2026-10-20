"""The copy of an expired session that lives outside the index.

Once a transcript expires the index holds the only record of what was said, and that is one
SQLite file on one disk with no snapshot behind it. These tests pin the second copy: written
at the moment the loss becomes permanent, readable without this tool.
"""

from __future__ import annotations

import argparse
import json

import pytest

from session_buddy.archive import default_archive_dir, read_archived_session
from session_buddy.database import IndexDatabase
from session_buddy.models import MessageRecord, SessionRecord


def indexed(db, session_id: str, source: str, text: str = "the decision table") -> None:
    db.upsert_session(
        SessionRecord(
            provider="claude", session_id=session_id, title=f"t-{session_id}", cwd="/repo",
            created_at=1, updated_at=2, git_branch="", source_path=source, file_mtime=1,
            file_size=1, message_count=1, first_prompt="", summary="", preview="",
            messages=[MessageRecord(provider="claude", session_id=session_id, idx=0,
                                    role="assistant", timestamp=1, text=text)],
        ),
        embedder=None,
    )
    db.conn.execute(
        "insert or replace into source_state (source_path, provider, session_id, file_mtime,"
        " file_size, indexed_at) values (?, 'claude', ?, 1, 1, 1)",
        (source, session_id),
    )
    db.conn.commit()


@pytest.fixture()
def db(tmp_path):
    database = IndexDatabase(tmp_path / "index.sqlite")
    yield database
    database.close()


def test_an_expiring_session_is_written_out_as_jsonl(db, tmp_path):
    indexed(db, "s1", "/tmp/s1.jsonl", text="compute() returns handled")
    root = tmp_path / "archive"

    db.mark_seen_sources(set(), prune=True, archive_dir=root)

    lines = (root / "claude" / "s1.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0]["kind"] == "session"
    assert records[0]["session_id"] == "s1"
    assert [r["text"] for r in records if r["kind"] == "message"] == ["compute() returns handled"]


def test_a_live_session_is_not_archived(db, tmp_path):
    """Only the expiring session is written: the archive is not a second copy of everything."""
    indexed(db, "gone", "/tmp/gone.jsonl")
    indexed(db, "here", "/tmp/here.jsonl")
    root = tmp_path / "archive"

    db.mark_seen_sources({"/tmp/here.jsonl"}, prune=True, archive_dir=root)

    assert (root / "claude" / "gone.jsonl").exists()
    assert not (root / "claude" / "here.jsonl").exists()


def test_a_subagent_id_does_not_become_a_directory(db, tmp_path):
    """Subagent ids carry a slash, which would split the file across a directory."""
    indexed(db, "parent/agent-abc", "/tmp/sub.jsonl")
    root = tmp_path / "archive"

    db.mark_seen_sources(set(), prune=True, archive_dir=root)

    assert (root / "claude" / "parent__agent-abc.jsonl").is_file()


def test_an_unwritable_archive_still_lets_the_index_flag_the_loss(db, tmp_path):
    """The flag is what stops the rows being deleted, so it must not depend on the archive."""
    indexed(db, "s1", "/tmp/s1.jsonl")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")

    assert db.mark_seen_sources(set(), prune=True, archive_dir=blocked) == 1

    assert db.count_missing_sources() == 1
    assert db.read_messages("claude", "s1") != []


def test_a_forgotten_session_is_still_readable_from_the_archive(db, tmp_path, capsys):
    from session_buddy.cli import cmd_show

    indexed(db, "s1", "/tmp/s1.jsonl", text="the only surviving copy")
    root = tmp_path / "archive"
    db.mark_seen_sources(set(), prune=True, archive_dir=root)
    db.forget_missing_sources()

    code = cmd_show(argparse.Namespace(db=db.path, session_id="s1", provider=None,
                                       limit=None, archive_dir=root))

    out = capsys.readouterr().out
    assert code == 0
    assert "the only surviving copy" in out
    assert "Read from the archive" in out


def test_the_archive_sits_next_to_the_index_by_default(tmp_path):
    assert default_archive_dir(tmp_path / "index.sqlite") == tmp_path / "archive"


def test_reading_an_absent_archive_is_not_an_error(tmp_path):
    assert read_archived_session(tmp_path / "nope", "s1") is None


def test_a_session_flagged_before_archiving_existed_is_caught_up(db, tmp_path):
    """The flag shipped first, so rows already marked have no archive until one is written."""
    indexed(db, "s1", "/tmp/s1.jsonl", text="flagged before the archive existed")
    db.mark_seen_sources(set(), prune=True)  # no archive_dir: the old behaviour
    root = tmp_path / "archive"
    assert not (root / "claude" / "s1.jsonl").exists()

    db.mark_seen_sources(set(), prune=True, archive_dir=root)

    assert "flagged before the archive existed" in (root / "claude" / "s1.jsonl").read_text()
