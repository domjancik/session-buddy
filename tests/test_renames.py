"""User-set session names.

Both harnesses let you rename a session, and both keep that name in a different place from
the title they derive themselves. People rename precisely when the derived title labels the
session badly, so dropping the name loses it on exactly the sessions that needed one.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from session_buddy.database import IndexDatabase
from session_buddy.indexer import index_all
from session_buddy.models import AnnotatedTitle
from session_buddy.parsers import load_codex_thread_metadata, parse_claude_session

FIRST_PROMPT = "why do you focus only on the naming, the whole point was the retry budget"
RENAME = "ACME-142 retry budget rework"


def write_claude_home(tmp_path: Path, **entry_overrides) -> Path:
    home = tmp_path / "claude"
    project = home / "projects" / "-repo-demo"
    project.mkdir(parents=True)
    session_id = "11111111-2222-3333-4444-555555555555"
    transcript = project / f"{session_id}.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"type": "user", "timestamp": "2026-09-16T10:00:00Z",
                 "message": {"role": "user", "content": FIRST_PROMPT}},
                {"type": "assistant", "timestamp": "2026-09-16T10:00:05Z",
                 "message": {"role": "assistant",
                             "content": [{"type": "text", "text": "Looking at the delivery gate."}]}},
            ]
        )
        + "\n"
    )
    entry = {
        "sessionId": session_id, "fullPath": str(transcript), "fileMtime": 1, "messageCount": 2,
        "firstPrompt": FIRST_PROMPT, "summary": "Naming discussion",
        "created": "2026-09-16T10:00:00Z", "modified": "2026-09-16T10:00:05Z",
        "gitBranch": "acme-142", "projectPath": "/repo/demo", "isSidechain": False,
    }
    entry.update(entry_overrides)
    (project / "sessions-index.json").write_text(json.dumps({"entries": [entry]}))
    return home


def claude_title(tmp_path: Path, **overrides) -> str:
    home = write_claude_home(tmp_path, **overrides)
    project = home / "projects" / "-repo-demo"
    entry = json.loads((project / "sessions-index.json").read_text())["entries"][0]
    record = parse_claude_session(Path(entry["fullPath"]), entry, entry["projectPath"])
    assert record is not None
    return record.title


def test_claude_rename_beats_the_first_prompt(tmp_path):
    assert claude_title(tmp_path, customTitle=RENAME) == RENAME


def test_claude_without_a_rename_still_uses_the_first_prompt(tmp_path):
    assert claude_title(tmp_path).startswith("why do you focus only on the naming")


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_rename_does_not_erase_the_title(tmp_path, blank):
    """An empty customTitle must not win, or the session ends up with no title at all."""
    assert claude_title(tmp_path, customTitle=blank).startswith("why do you focus only")


def write_codex_db(path: Path, *, with_name_column: bool, name: str | None = None) -> None:
    columns = ("id text primary key, title text, cwd text, created_at_ms integer, "
               "updated_at_ms integer, git_branch text, preview text")
    if with_name_column:
        columns += ", name text"
    conn = sqlite3.connect(path)
    conn.execute(f"create table threads ({columns})")
    values = ["t1", "is linear accessible", "/repo", 1, 2, "main", ""]
    placeholders = "?,?,?,?,?,?,?"
    if with_name_column:
        values.append(name)
        placeholders += ",?"
    conn.execute(f"insert into threads values ({placeholders})", values)
    conn.commit()
    conn.close()


def test_codex_rename_is_read_from_the_threads_table(tmp_path):
    write_codex_db(tmp_path / "state_5.sqlite", with_name_column=True,
                   name="Check Linear accessibility")

    meta = load_codex_thread_metadata(tmp_path)

    assert meta["t1"]["name"] == "Check Linear accessibility"
    assert meta["t1"]["title"] == "is linear accessible"  # Codex's own summary, still available


def test_an_older_codex_schema_without_name_still_reads(tmp_path):
    """`name` arrived in a later release; selecting it blindly loses every thread."""
    write_codex_db(tmp_path / "state_5.sqlite", with_name_column=False)

    meta = load_codex_thread_metadata(tmp_path)

    assert meta["t1"]["title"] == "is linear accessible"
    assert "name" not in meta["t1"]


def test_the_rename_survives_an_annotator_title_being_withdrawn(tmp_path):
    """A withdrawn annotator title must revert to the user's name, not the first prompt."""
    home = write_claude_home(tmp_path, customTitle=RENAME)
    db_path = tmp_path / "index.sqlite"
    index_all(db_path, claude_home=home, codex_home=tmp_path / "missing",
              annotators=[], semantic=False)

    db = IndexDatabase(db_path)
    try:
        session_id = "11111111-2222-3333-4444-555555555555"
        db.apply_annotated_titles([AnnotatedTitle("claude", session_id, "traycer", "Epic tab", 10)])
        assert db.conn.execute("select title from sessions").fetchone()["title"] == "Epic tab"

        db.apply_annotated_titles([])

        row = db.conn.execute("select title, title_source from sessions").fetchone()
        assert row["title"] == RENAME
        assert row["title_source"] == "provider"
    finally:
        db.close()


def test_the_codex_rename_becomes_the_session_title(tmp_path):
    """End to end: the name reaches record.title, not just the metadata dict."""
    from session_buddy.parsers import parse_codex_session

    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps({
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": FIRST_PROMPT}],
    }) + "\n")
    meta = {"id": "t1", "title": "is linear accessible", "name": "Check Linear accessibility",
            "cwd": "/repo", "created_at_ms": 1, "updated_at_ms": 2, "git_branch": "main",
            "preview": ""}

    record = parse_codex_session(transcript, meta, None)

    assert record is not None
    assert record.title == "Check Linear accessibility"


@pytest.mark.parametrize(
    "name,derived,expected,why",
    [
        ("Check Linear accessibility", "is linear accessible",
         "Check Linear accessibility", "a real rename wins"),
        ("do we have a script that regens all", "do we have a script that regens all events values",
         "do we have a script that regens all events values", "a truncation loses"),
        ("study bun run styleguide and chargef", "study bun run styleguide  and chargeflow.io design",
         "study bun run styleguide  and chargeflow.io design", "whitespace differs, still a truncation"),
        ("DO WE HAVE A SCRIPT", "do we have a script that regens all",
         "do we have a script that regens all", "case differs, still a truncation"),
        ("", "fallback", "fallback", "no name at all"),
        ("   ", "fallback", "fallback", "a blank name"),
        ("Same", "Same", "Same", "equal, not a truncation - the name stands"),
        ("Longer name entirely", "", "Longer name entirely", "no derived title to fall back to"),
    ],
)
def test_prefer_rename(name, derived, expected, why):
    from session_buddy.parsers import prefer_rename

    assert prefer_rename(name, derived) == expected, why


def test_a_prefilled_codex_rename_does_not_shorten_the_title(tmp_path):
    """Codex's rename box pre-fills from the prompt; accepting it stores a mid-sentence cut."""
    from session_buddy.parsers import parse_codex_session

    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps({
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": FIRST_PROMPT}],
    }) + "\n")
    meta = {"id": "t1", "title": "do we have a script that regens all events values for all services",
            "name": "do we have a script that regens all", "cwd": "/repo",
            "created_at_ms": 1, "updated_at_ms": 2, "git_branch": "main", "preview": ""}

    record = parse_codex_session(transcript, meta, None)

    assert record is not None
    assert record.title == "do we have a script that regens all events values for all services"
