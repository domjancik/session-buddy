from __future__ import annotations

from pathlib import Path

import pytest

from session_buddy.annotators.base import AnnotatorResult
from session_buddy.database import IndexDatabase
from session_buddy.indexer import IndexStats, run_annotators
from session_buddy.models import AnnotatedTitle, Annotation, SessionRecord
from session_buddy.search import parse_ext_filter, search_sessions


def make_session(db: IndexDatabase, provider: str, session_id: str, title: str) -> None:
    db.upsert_session(
        SessionRecord(
            provider=provider,
            session_id=session_id,
            title=title,
            cwd="/repo",
            created_at=1,
            updated_at=2,
            git_branch="main",
            source_path=f"/tmp/{session_id}.jsonl",
            file_mtime=1,
            file_size=1,
            message_count=1,
            first_prompt=title,
            summary="",
            preview="webhook retry work",
        ),
        embedder=None,
    )


class FakeAnnotator:
    def __init__(self, source, annotations=(), titles=(), priority=0, explode=False, present=True):
        self.source = source
        self.title_priority = priority
        self._annotations = list(annotations)
        self._titles = list(titles)
        self._explode = explode
        self._present = present

    def detect(self, home: Path | None = None) -> bool:
        return self._present

    def collect(self, home: Path | None = None) -> AnnotatorResult:
        if self._explode:
            raise RuntimeError("store unreadable")
        return AnnotatorResult(annotations=self._annotations, titles=self._titles)


@pytest.fixture()
def db(tmp_path):
    database = IndexDatabase(tmp_path / "index.sqlite")
    make_session(database, "claude", "s1", "<task-notification> noise")
    yield database
    database.close()


def test_annotations_survive_session_reindex(db):
    db.replace_annotations("traycer", [Annotation("claude", "s1", "traycer", "epic_title", "Alpha")])

    make_session(db, "claude", "s1", "<task-notification> noise")  # re-parse the transcript

    assert db.annotations_for("claude", "s1") == {"traycer": {"epic_title": "Alpha"}}


def test_replace_annotations_is_wholesale_per_source(db):
    db.replace_annotations("traycer", [Annotation("claude", "s1", "traycer", "epic_title", "Old")])
    db.replace_annotations("omnigent", [Annotation("claude", "s1", "omnigent", "swarm", "S")])
    db.replace_annotations("traycer", [Annotation("claude", "s1", "traycer", "epic_title", "New")])

    grouped = db.annotations_for("claude", "s1")
    assert grouped["traycer"] == {"epic_title": "New"}
    assert grouped["omnigent"] == {"swarm": "S"}  # untouched by the traycer refresh


def test_annotator_title_overrides_provider_title(db):
    db.apply_annotated_titles([AnnotatedTitle("claude", "s1", "traycer", "ACME-142 retry gate", 10)])

    row = db.conn.execute("select title, provider_title, title_source from sessions").fetchone()
    assert row["title"] == "ACME-142 retry gate"
    assert row["provider_title"] == "<task-notification> noise"
    assert row["title_source"] == "traycer"


def test_withdrawn_title_reverts_to_provider_title(db):
    db.apply_annotated_titles([AnnotatedTitle("claude", "s1", "traycer", "Temp", 10)])

    db.apply_annotated_titles([])

    row = db.conn.execute("select title, title_source from sessions").fetchone()
    assert row["title"] == "<task-notification> noise"
    assert row["title_source"] == "provider"


def test_highest_priority_annotator_wins_the_title(db):
    db.apply_annotated_titles(
        [
            AnnotatedTitle("claude", "s1", "low", "Low priority", 1),
            AnnotatedTitle("claude", "s1", "high", "High priority", 99),
        ]
    )

    assert db.conn.execute("select title_source from sessions").fetchone()["title_source"] == "high"


def test_blank_annotator_title_never_wins(db):
    db.apply_annotated_titles([AnnotatedTitle("claude", "s1", "traycer", "   ", 10)])

    assert db.conn.execute("select title_source from sessions").fetchone()["title_source"] == "provider"


def test_failing_annotator_does_not_break_indexing(db):
    stats = IndexStats()

    run_annotators(
        db,
        stats,
        annotators=[
            FakeAnnotator("broken", explode=True),
            FakeAnnotator("ok", annotations=[Annotation("claude", "s1", "ok", "k", "v")]),
        ],
    )

    assert stats.annotation_sources == ["ok"]
    assert any("broken" in w for w in stats.annotation_warnings)
    assert db.annotations_for("claude", "s1") == {"ok": {"k": "v"}}


def test_absent_tool_is_skipped_silently(db):
    stats = IndexStats()

    run_annotators(db, stats, annotators=[FakeAnnotator("traycer", present=False)])

    assert stats.annotation_sources == []
    assert stats.annotation_warnings == []


def test_ext_filter_scopes_results(db, tmp_path):
    make_session(db, "claude", "s2", "other session")
    db.replace_annotations("traycer", [Annotation("claude", "s1", "traycer", "epic_title", "Alpha Epic")])
    db.conn.commit()

    hits = search_sessions(db.path, "webhook", ext=["traycer.epic_title=Alpha"], semantic=False)

    assert [h.session_id for h in hits] == ["s1"]


def test_ext_filter_by_source_only(db):
    make_session(db, "claude", "s2", "other session")
    db.replace_annotations("traycer", [Annotation("claude", "s1", "traycer", "epic_id", "e1")])
    db.conn.commit()

    assert [h.session_id for h in search_sessions(db.path, "webhook", ext=["traycer"], semantic=False)] == ["s1"]
    assert search_sessions(db.path, "webhook", ext=["omnigent"], semantic=False) == []


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("traycer", ("traycer", "", "")),
        ("traycer.epic_id", ("traycer", "epic_id", "")),
        ("traycer.epic_title=Payments*", ("traycer", "epic_title", "Payments%")),
    ],
)
def test_parse_ext_filter(expr, expected):
    assert parse_ext_filter(expr) == expected
