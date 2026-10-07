"""Things the index was quietly losing.

Each of these was found by asking why a session that existed could not be answered for:
a Traycer store that upgraded its schema, a subagent transcript that overwrote its parent,
and a pruned session whose branch rows outlived it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from session_buddy.annotators.traycer import read_chat_rows, read_chat_store
from session_buddy.database import IndexDatabase
from session_buddy.models import MessageRecord, SessionRecord
from session_buddy.parsers import parse_claude_session
from session_buddy.resume import build_resume_command

AGENT = {"harnessId": "claude", "harnessSessionId": "session-9", "id": "agent-9",
         "title": "ws-retry", "parentId": None, "workspaceFolders": json.dumps(["/repo"])}
CHAIN = {"harnessId": "claude", "sessionId": "session-gui",
         "sessionWorkspaceSnapshot": {"primaryWorkspace": "/repo/gui"}}


def head_store(path: Path) -> Path:
    """A chat.db in the newer columnar shape."""
    db = path / "chat.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "create table chat_projection_head (chat_id text, through_seq int, tenant_kind text,"
        " parent_chat_id text, title text, created_at int, updated_at int,"
        " tui_agent_json text, host_private_json text)"
    )
    conn.execute(
        "insert into chat_projection_head values (?,1,'tui-agent',NULL,?,1,2,?,?)",
        ("chat-9", "ws-retry", json.dumps(AGENT), json.dumps({"revision": 0, "data": {}})),
    )
    conn.execute(
        "insert into chat_projection_head values (?,1,'chat',NULL,?,1,3,NULL,?)",
        ("chat-gui", "Dispute Status CSV", json.dumps({"revision": 0, "data": {"activeSessionChain": CHAIN}})),
    )
    conn.commit()
    conn.close()
    return db


def legacy_store(path: Path) -> Path:
    """The older blob shape, which older Traycer installs still have."""
    db = path / "chat.db"
    conn = sqlite3.connect(db)
    conn.execute("create table chat_projection (chat_id text, projection_json text)")
    conn.execute(
        "insert into chat_projection values ('chat-9', ?)",
        (json.dumps({"chatId": "chat-9", "tenantKind": "tui-agent", "title": "ws-retry",
                     "updatedAt": 2, "tuiAgent": AGENT, "messages": []}),),
    )
    conn.commit()
    conn.close()
    return db


def test_the_newer_chat_table_is_read(tmp_path):
    """`chat_projection` became `chat_projection_head`; selecting the old name read nothing."""
    record = read_chat_store(head_store(tmp_path), "epic-9", "Epic")

    bound = {b["session_id"]: b for b in record["bindings"]}
    assert bound["session-9"]["kind"] == "agent"
    assert bound["session-9"]["title"] == "ws-retry"
    assert bound["session-gui"]["kind"] == "chat"
    assert bound["session-gui"]["workspace"] == "/repo/gui"


def test_the_older_chat_table_still_reads(tmp_path):
    """A store that has not upgraded must keep working."""
    record = read_chat_store(legacy_store(tmp_path), "epic-9", "Epic")

    assert [b["session_id"] for b in record["bindings"]] == ["session-9"]


def test_a_store_with_neither_table_is_empty_not_an_error(tmp_path):
    db = tmp_path / "chat.db"
    sqlite3.connect(db).execute("create table unrelated (x int)")

    conn = sqlite3.connect(db)
    try:
        assert read_chat_rows(conn) == []
    finally:
        conn.close()


# --- subagent transcripts -----------------------------------------------------------------

def write_claude_session(tmp_path: Path, *, subagent: bool) -> Path:
    project = tmp_path / "projects" / "-repo"
    parent = "11111111-2222-3333-4444-555555555555"
    directory = project / parent / "subagents" if subagent else project
    directory.mkdir(parents=True, exist_ok=True)
    name = "agent-abc123.jsonl" if subagent else f"{parent}.jsonl"
    rows = [
        {"type": "user", "timestamp": "2026-10-07T10:00:00Z", "sessionId": parent, "cwd": "/repo",
         "message": {"role": "user", "content": "subagent work" if subagent else "parent work"}},
    ]
    path = directory / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_a_subagent_gets_its_own_id(tmp_path):
    """It records the PARENT's sessionId, so keyed on that it overwrote the parent row."""
    parent = parse_claude_session(write_claude_session(tmp_path, subagent=False), None, "/repo")
    child = parse_claude_session(write_claude_session(tmp_path, subagent=True), None, "/repo")

    assert parent is not None and child is not None
    assert child.session_id != parent.session_id
    assert child.session_id.startswith(parent.session_id)
    assert child.session_id.endswith("agent-abc123")


def test_a_subagent_does_not_evict_its_parent(tmp_path):
    db = IndexDatabase(tmp_path / "index.sqlite")
    try:
        for subagent in (False, True):
            record = parse_claude_session(write_claude_session(tmp_path, subagent=subagent), None, "/repo")
            db.upsert_session(record, embedder=None)

        rows = db.conn.execute("select count(*) from sessions").fetchone()[0]
        assert rows == 2, "both the parent and its subagent must survive one index pass"
    finally:
        db.close()


def test_resume_addresses_the_parent_of_a_subagent():
    command = build_resume_command("claude", "11111111-2222/agent-abc123", "/repo")

    assert command.argv == ["claude", "--resume", "11111111-2222"]


# --- prune --------------------------------------------------------------------------------

def test_deleting_a_session_takes_its_branches(tmp_path):
    """Orphaned branch rows made `--branch` match sessions no join could resolve."""
    db = IndexDatabase(tmp_path / "index.sqlite")
    try:
        record = SessionRecord(
            provider="claude", session_id="s1", title="t", cwd="/repo", created_at=1,
            updated_at=2, git_branch="acme-142", source_path="/tmp/s1.jsonl", file_mtime=1,
            file_size=1, message_count=1, first_prompt="", summary="", preview="",
            messages=[MessageRecord(provider="claude", session_id="s1", idx=0, role="assistant",
                                    timestamp=1, text="git checkout -b feat/x")],
        )
        db.upsert_session(record, embedder=None)
        assert db.conn.execute("select count(*) from session_branches").fetchone()[0] > 0

        db.delete_session("claude", "s1")

        assert db.conn.execute("select count(*) from session_branches").fetchone()[0] == 0
    finally:
        db.close()
