from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from session_buddy.database import IndexDatabase
from session_buddy.indexer import check_index_freshness_db, index_all, should_skip
from session_buddy.parsers import (
    opencode_db_path,
    opencode_metadata_fingerprint,
    opencode_source_path,
    opencode_stat_key,
    parse_opencode_session,
    scan_opencode_sessions,
)
from session_buddy.resume import build_resume_command

SCHEMA = """
create table session (
    id text primary key, project_id text, parent_id text, slug text, directory text,
    title text, time_created integer, time_updated integer, time_archived integer,
    agent text, model text
);
create table message (id text primary key, session_id text, time_created integer, data text);
create table part (id text primary key, message_id text, session_id text, time_created integer, data text);
"""


def make_db(tmp_path: Path, sessions=None, messages=None, parts=None) -> Path:
    home = tmp_path / "opencode"
    home.mkdir(parents=True, exist_ok=True)
    db_path = opencode_db_path(home)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "insert into session (id, directory, title, time_created, time_updated) values (?, ?, ?, ?, ?)",
        sessions
        or [("ses_1", "/repo", "Refactor the billing gate", 1000, 2000)],
    )
    conn.executemany(
        "insert into message (id, session_id, time_created, data) values (?, ?, ?, ?)",
        messages
        or [
            ("msg_1", "ses_1", 1000, json.dumps({"role": "user"})),
            ("msg_2", "ses_1", 1100, json.dumps({"role": "assistant"})),
        ],
    )
    conn.executemany(
        "insert into part (id, message_id, session_id, time_created, data) values (?, ?, ?, ?, ?)",
        parts
        or [
            ("prt_1", "msg_1", "ses_1", 1000, json.dumps({"type": "text", "text": "why is billing failing"})),
            ("prt_2", "msg_1", "ses_1", 1001, json.dumps({"type": "tool", "tool": "bash"})),
            ("prt_3", "msg_2", "ses_1", 1100, json.dumps({"type": "text", "text": "the gate rejects it"})),
        ],
    )
    conn.commit()
    conn.close()
    return home


def test_scan_returns_nothing_without_a_database(tmp_path):
    assert scan_opencode_sessions(tmp_path / "absent") == []


def test_parse_builds_a_session_from_the_database(tmp_path):
    home = make_db(tmp_path)
    row = scan_opencode_sessions(home)[0]

    record = parse_opencode_session(opencode_db_path(home), row)

    assert record.provider == "opencode"
    assert record.session_id == "ses_1"
    assert record.title == "Refactor the billing gate"
    assert record.cwd == "/repo"
    assert record.created_at == 1000
    assert record.updated_at == 2000
    assert [m.role for m in record.messages] == ["user", "assistant"]
    assert record.first_prompt == "why is billing failing"


def test_non_text_parts_are_excluded(tmp_path):
    home = make_db(tmp_path)
    row = scan_opencode_sessions(home)[0]

    record = parse_opencode_session(opencode_db_path(home), row)

    assert "bash" not in " ".join(m.text for m in record.messages)


def test_messages_without_text_are_dropped(tmp_path):
    home = make_db(
        tmp_path,
        messages=[("msg_1", "ses_1", 1000, json.dumps({"role": "user"}))],
        parts=[("prt_1", "msg_1", "ses_1", 1000, json.dumps({"type": "tool", "tool": "bash"}))],
    )
    row = scan_opencode_sessions(home)[0]

    record = parse_opencode_session(opencode_db_path(home), row)

    assert record.messages == []
    assert record.message_count == 0


def test_title_falls_back_to_the_first_prompt(tmp_path):
    home = make_db(tmp_path, sessions=[("ses_1", "/repo", "", 1000, 2000)])
    row = scan_opencode_sessions(home)[0]

    assert parse_opencode_session(opencode_db_path(home), row).title == "why is billing failing"


def test_source_path_is_unique_per_session_in_a_shared_database(tmp_path):
    db = opencode_db_path(tmp_path)

    assert opencode_source_path(db, "ses_1") != opencode_source_path(db, "ses_2")
    assert opencode_source_path(db, "ses_1").endswith("#ses_1")


def test_change_key_tracks_the_session_not_the_database(tmp_path):
    """The whole point: one session's write must not invalidate its neighbours."""
    home = make_db(
        tmp_path,
        sessions=[("ses_1", "/repo", "One", 1000, 2000), ("ses_2", "/repo", "Two", 1000, 3000)],
    )
    db = IndexDatabase(tmp_path / "index.sqlite")
    index_all(db_path=tmp_path / "index.sqlite", claude_home=tmp_path / "none",
              codex_home=tmp_path / "none", opencode_home=home, pi_home=tmp_path / "none",
              semantic=False)
    db.close()
    db = IndexDatabase(tmp_path / "index.sqlite")

    # ses_2 gets written; the database mtime moves for both.
    conn = sqlite3.connect(opencode_db_path(home))
    conn.execute("update session set time_updated = 4000 where id = 'ses_2'")
    conn.commit()
    conn.close()

    rows = {r["id"]: r for r in scan_opencode_sessions(home)}
    unchanged = should_skip(
        db,
        Path(opencode_source_path(opencode_db_path(home), "ses_1")),
        force=False,
        metadata_fingerprint=opencode_metadata_fingerprint(rows["ses_1"]),
        stat_key=opencode_stat_key(rows["ses_1"]),
    )
    changed = should_skip(
        db,
        Path(opencode_source_path(opencode_db_path(home), "ses_2")),
        force=False,
        metadata_fingerprint=opencode_metadata_fingerprint(rows["ses_2"]),
        stat_key=opencode_stat_key(rows["ses_2"]),
    )
    db.close()

    assert unchanged is True
    assert changed is False


def test_index_and_freshness_round_trip(tmp_path):
    home = make_db(tmp_path)
    index_path = tmp_path / "index.sqlite"
    empty = tmp_path / "none"

    stats = index_all(db_path=index_path, claude_home=empty, codex_home=empty,
                      opencode_home=home, pi_home=empty, semantic=False)
    assert stats.indexed == 1

    db = IndexDatabase(index_path)
    try:
        fresh = check_index_freshness_db(db, empty, empty, home, empty)
        assert fresh.stale is False
        assert fresh.unchanged == 1
    finally:
        db.close()


def test_resume_command_uses_the_session_flag():
    command = build_resume_command("opencode", "ses_1", "/repo")

    assert command.argv == ["opencode", "--session", "ses_1"]
    assert command.shell_line().startswith("cd /repo && ")


def test_a_corrupt_database_does_not_raise(tmp_path):
    home = tmp_path / "opencode"
    home.mkdir()
    opencode_db_path(home).write_bytes(b"not a database")

    assert scan_opencode_sessions(home) == []
