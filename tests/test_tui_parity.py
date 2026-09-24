"""The TUI must show what the CLI shows.

Branches and annotator metadata were CLI-only for five releases because the CLI grew the
formatting inline. Both surfaces now render through `detail_lines`, and the first test here
fails if either one starts formatting its own.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from session_buddy.database import IndexDatabase
from session_buddy.details import detail_lines, title_origin
from session_buddy.models import Annotation, MessageRecord, SessionRecord

pytest.importorskip("textual", reason="TUI extra not installed")

from session_buddy.tui import SessionSearchApp  # noqa: E402


SESSION = "11111111-2222-3333-4444-555555555555"


class _Sink:
    """Stands in for a Static widget outside a running app, recording what it was given."""

    def __init__(self) -> None:
        self.text = ""

    def update(self, value="", *_args, **_kwargs) -> None:
        self.text = str(value)


def seeded_db(tmp_path: Path, *, renamed: bool = False) -> Path:
    db = IndexDatabase(tmp_path / "index.sqlite")
    try:
        db.upsert_session(
            SessionRecord(
                provider="claude", session_id=SESSION, title="ACME-142 retry gate",
                cwd="/repo/acme", created_at=1, updated_at=2,
                git_branch="acme-142-start", source_path="/tmp/s.jsonl",
                file_mtime=1, file_size=1, message_count=1,
                first_prompt="webhook retry budget", summary="", preview="webhook retry budget",
                renamed_by_user=renamed, titled=renamed,
                messages=[MessageRecord(provider="claude", session_id=SESSION, idx=0,
                                        role="assistant", timestamp=1,
                                        text=" * [new branch] acme-142-pushed -> acme-142-pushed")],
            ),
            embedder=None,
        )
        db.replace_annotations("traycer", [
            Annotation("claude", SESSION, "traycer", "epic_title", "Payments"),
            Annotation("claude", SESSION, "traycer", "agent_title", "ACME-142 retry gate"),
        ])
        db.conn.commit()
        return db.path
    finally:
        db.close()


def test_the_tui_preview_renders_the_same_details_as_the_cli(tmp_path):
    """Both surfaces call one renderer, so a new fact appears in both or neither."""
    path = seeded_db(tmp_path)
    app = SessionSearchApp(path, "webhook")
    db = IndexDatabase(path)
    try:
        expected = detail_lines(db, "claude", SESSION)
    finally:
        db.close()

    class Result:
        provider, session_id = "claude", SESSION

    assert app.detail_lines_for(Result()) == expected
    assert any(line.startswith("branches:") for line in expected)
    assert any(line.startswith("traycer:") for line in expected)


def test_the_preview_actually_renders_the_details(tmp_path, monkeypatch):
    """Asserting the helper is not enough: the preview has to call it and show the result."""
    from session_buddy.search import search_sessions

    path = seeded_db(tmp_path)
    panel = _Sink()
    monkeypatch.setattr(SessionSearchApp, "preview_panel", property(lambda self: panel))
    app = SessionSearchApp(path, "webhook")
    app.results = search_sessions(path, "webhook", semantic=False)
    assert app.results, "fixture must produce a hit for the preview to render"

    app.refresh_preview()

    assert "branches:" in panel.text
    assert "acme-142-pushed (push)" in panel.text
    assert "traycer:" in panel.text
    assert "epic_title=Payments" in panel.text
    assert "started on: acme-142-start" in panel.text  # labelled, not called "branch"


def test_the_details_include_a_branch_the_session_did_not_start_on(tmp_path):
    """The old preview showed only git_branch, which is where the session STARTED."""
    path = seeded_db(tmp_path)
    db = IndexDatabase(path)
    try:
        lines = detail_lines(db, "claude", SESSION)
    finally:
        db.close()

    branches = next(line for line in lines if line.startswith("branches:"))
    assert "acme-142-pushed (push)" in branches
    assert "acme-142-start (start)" in branches


def test_the_tui_passes_its_filters_to_the_search(tmp_path, monkeypatch):
    """--ext and --branch were CLI-only; the TUI dropped them silently."""
    seen: dict[str, object] = {}

    def fake_search(db_path, query, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr("session_buddy.tui.search_sessions", fake_search)
    # run_search writes to live widgets; stub the view so the test drives the query path only.
    monkeypatch.setattr(SessionSearchApp, "status_panel", property(lambda self: _Sink()))
    monkeypatch.setattr(SessionSearchApp, "populate_results", lambda self: None)
    monkeypatch.setattr(SessionSearchApp, "refresh_preview", lambda self: None)
    app = SessionSearchApp(seeded_db(tmp_path), ext=["traycer.epic_title=Payments"], branch="acme-142*")

    app.run_search("webhook")

    assert seen["ext"] == ["traycer.epic_title=Payments"]
    assert seen["branch"] == "acme-142*"


def test_run_tui_keeps_index_status_out_of_the_filters(tmp_path, monkeypatch):
    """The app was constructed positionally, so a new parameter would swallow the status."""
    captured: dict[str, object] = {}

    class FakeApp:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def run(self):
            return None

    monkeypatch.setattr("session_buddy.tui.SessionSearchApp", FakeApp)
    from session_buddy.tui import run_tui

    run_tui(tmp_path / "i.sqlite", "q", index_status="Index is fresh.")

    assert captured["index_status"] == "Index is fresh."
    assert captured["ext"] is None


@pytest.mark.parametrize(
    "source,expected",
    [("user", "renamed"), ("traycer", "traycer"), ("conductor", "conductor"), ("provider", ""), ("", "")],
)
def test_title_origin(source, expected):
    assert title_origin(source) == expected


def test_the_tui_command_accepts_the_same_filters_as_search():
    from session_buddy.cli import build_parser

    args = build_parser().parse_args(
        ["tui", "--ext", "traycer.epic_title=Payments", "--branch", "acme-142"]
    )

    assert args.ext == ["traycer.epic_title=Payments"]
    assert args.branch == "acme-142"


def test_cmd_tui_tolerates_a_namespace_without_the_new_filters(monkeypatch, tmp_path):
    """A caller that builds its own namespace must not break on a newly added filter."""
    from session_buddy import cli

    monkeypatch.setattr(cli, "run_command_in_new_tmux_session_if_available", lambda *a, **k: None)
    monkeypatch.setattr(cli, "handle_freshness_for_process", lambda *a, **k: "fresh")
    monkeypatch.setattr(cli, "run_tui", lambda *a, **k: None)
    args = argparse.Namespace(
        db=tmp_path / "i.sqlite", query="", provider=None, cwd=None, no_tmux=True,
        auto_index=False, _session_buddy_argv=["sb", "tui"],
    )

    assert cli.cmd_tui(args) == 0
