"""Traycer's chat.db store.

Deliberately free of `pycrdt`: this half of Traycer is plain SQLite and must keep working
when the optional extra is not installed.
"""

from __future__ import annotations

import json
import sqlite3

from session_buddy.annotators.traycer import TraycerAnnotator, read_chat_store

AGENT = {
    "agentMode": "regular",
    "id": "agent-9",
    "harnessId": "claude",
    "harnessSessionId": "session-9",
    "title": "ws-retry-semantics",
    "parentId": "agent-parent",
    "workspaceFolders": json.dumps(["/repo/worktree-a", "/repo/worktree-b"]),
}


def write_chat_store(root, epic_id, chats, *, wal=False):
    """Build a chat.db shaped like Traycer's event-sourced projection table."""
    db_path = root / "host" / "epic-state" / epic_id / "chat" / "chat.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    if wal:
        conn.execute("pragma journal_mode=wal")
    conn.execute(
        "create table if not exists chat_projection "
        "(chat_id text primary key, through_seq integer, projection_json text, "
        " created_at integer, updated_at integer, owner_user_id text)"
    )
    conn.executemany(
        "insert or replace into chat_projection values (?, 1, ?, 0, 0, 'u')",
        [(chat["chatId"], json.dumps(chat)) for chat in chats],
    )
    conn.commit()
    conn.close()
    return db_path


def chat(**overrides):
    record = {
        "chatId": "chat-9",
        "tenantKind": "tui-agent",
        "parentChatId": None,
        "title": "ws-retry-semantics",
        "updatedAt": 1700000000000,
        "lifecycle": {"state": "active", "archivedAt": None, "deletedAt": None},
        "messages": [],
        "tuiAgent": dict(AGENT),
    }
    record.update(overrides)
    return record


def test_a_session_bound_only_in_the_chat_store_is_found(tmp_path):
    """The gap this store closes: no seed exists, so the seeds pass contributes nothing."""
    write_chat_store(tmp_path, "epic-9", [chat()])

    result = TraycerAnnotator().collect(tmp_path)

    pairs = {(a.key, a.value) for a in result.annotations}
    assert ("agent_title", "ws-retry-semantics") in pairs
    assert ("epic_id", "epic-9") in pairs
    assert ("kind", "agent") in pairs  # every chat.db row is a tui-agent tenant
    assert ("parent_id", "agent-parent") in pairs
    assert ("workspace", "/repo/worktree-a") in pairs
    assert {(a.provider, a.session_id) for a in result.annotations} == {("claude", "session-9")}
    assert [t.title for t in result.titles] == ["ws-retry-semantics"]


def test_detect_accepts_a_machine_with_only_chat_stores(tmp_path):
    assert TraycerAnnotator().detect(tmp_path) is False

    write_chat_store(tmp_path, "epic-9", [chat()])

    assert TraycerAnnotator().detect(tmp_path) is True


def test_a_chat_with_no_terminal_agent_binds_nothing(tmp_path):
    write_chat_store(tmp_path, "epic-9", [chat(tuiAgent=None)])

    result = TraycerAnnotator().collect(tmp_path)

    assert result.annotations == []
    assert result.groups == []  # an epic that binds no session is not worth listing


def test_an_unreadable_store_does_not_lose_the_others(tmp_path):
    write_chat_store(tmp_path, "epic-good", [chat()])
    bad = tmp_path / "host" / "epic-state" / "epic-bad" / "chat"
    bad.mkdir(parents=True)
    (bad / "chat.db").write_bytes(b"not a sqlite database")

    result = TraycerAnnotator().collect(tmp_path)

    assert [g["id"] for g in result.groups] == ["epic-good"]
    assert "chat store(s) could not be read" in result.warning


def test_a_malformed_projection_does_not_lose_its_epic(tmp_path):
    db = write_chat_store(tmp_path, "epic-9", [chat()])
    conn = sqlite3.connect(db)
    conn.execute("insert into chat_projection values ('broken', 1, '{not json', 0, 0, 'u')")
    conn.commit()
    conn.close()

    result = TraycerAnnotator().collect(tmp_path)

    assert {a.session_id for a in result.annotations} == {"session-9"}


def test_the_group_carries_the_epic_id_and_latest_update(tmp_path):
    write_chat_store(
        tmp_path,
        "epic-9",
        [chat(), chat(chatId="chat-10", updatedAt=1800000000000,
                      tuiAgent={**AGENT, "id": "agent-10", "harnessSessionId": "session-10"})],
    )

    groups = TraycerAnnotator().collect(tmp_path).groups

    assert groups == [
        {"id": "epic-9", "title": "", "updated_at": "1800000000000", "sessions": "2",
         "key": "epic_id"}
    ]


def test_the_chat_store_reads_uncheckpointed_wal_content(tmp_path):
    """Traycer runs in WAL mode while it is live; a read that ignores the -wal under-reports."""
    db = write_chat_store(tmp_path, "epic-9", [chat()], wal=True)
    conn = sqlite3.connect(db)  # left open, so the write stays in the -wal
    conn.execute(
        "insert into chat_projection values ('chat-11', 1, ?, 0, 0, 'u')",
        (json.dumps(chat(chatId="chat-11",
                         tuiAgent={**AGENT, "id": "a11", "harnessSessionId": "session-11"})),),
    )
    conn.commit()
    try:
        record = read_chat_store(db, "epic-9", "Epic")
    finally:
        conn.close()

    assert {b["session_id"] for b in record["bindings"]} == {"session-9", "session-11"}


CHAIN = {
    "harnessId": "claude",
    "sessionId": "session-gui",
    "profileId": None,
    "sessionWorkspaceSnapshot": {
        "primaryWorkspace": "/repo/.worktrees/72-icy",
        "secondaryWorkspaces": ["/elsewhere"],
        "workspaceKind": "session-snapshot",
    },
}


def gui_chat(*, chain=None, messages=(), **overrides):
    """A desktop-app chat: `tenantKind: chat`, no tuiAgent, binding under hostPrivate."""
    record = chat(
        chatId="chat-gui",
        tenantKind="chat",
        title="Dispute Status CSV Generation",
        tuiAgent=None,
        messages=list(messages),
        hostPrivate={"revision": 0, "data": {"activeSessionChain": chain or dict(CHAIN)}},
    )
    record.update(overrides)
    return record


def test_a_desktop_chat_is_bound_through_hostprivate(tmp_path):
    """These have no tuiAgent at all; reading only that key loses every GUI chat."""
    write_chat_store(tmp_path, "epic-9", [gui_chat()])

    result = TraycerAnnotator().collect(tmp_path)

    pairs = {(a.key, a.value) for a in result.annotations}
    assert ("chat_title", "Dispute Status CSV Generation") in pairs
    assert ("chat_id", "chat-gui") in pairs
    assert ("kind", "chat") in pairs  # not agent: the seeds' chat_* keys, not agent_*
    assert ("workspace", "/repo/.worktrees/72-icy") in pairs
    assert {(a.provider, a.session_id) for a in result.annotations} == {("claude", "session-gui")}
    assert [t.title for t in result.titles] == ["Dispute Status CSV Generation"]


def test_a_top_level_session_chain_is_not_where_the_binding_lives(tmp_path):
    """The trap: probing the top level finds nothing and reads as 'no GUI chats here'.

    The Yjs `chats[]` entries keep `activeSessionChain` at the top level; chat.db nests it
    under `hostPrivate.data`. A top-level probe against this store is silently empty.
    """
    stray = chat(chatId="chat-gui", tenantKind="chat", tuiAgent=None,
                 activeSessionChain=dict(CHAIN), hostPrivate={"revision": 0, "data": {}})
    write_chat_store(tmp_path, "epic-9", [stray])

    assert TraycerAnnotator().collect(tmp_path).annotations == []


def test_earlier_sessions_of_a_resumed_chat_are_bound_too(tmp_path):
    """A resumed chat leaves its previous sessions only in each message's sessionAnchor."""
    anchor = {"harnessId": "claude", "sessionId": "session-earlier",
              "sessionWorkspaceSnapshot": {"primaryWorkspace": "/repo/old"}}
    messages = [
        {"id": "m1", "body": {"sessionAnchor": anchor}},
        {"id": "m2", "body": {"sessionAnchor": dict(CHAIN)}},   # the live one, already bound
        {"id": "m3", "body": {"sessionAnchor": anchor}},        # repeated, must not duplicate
    ]
    write_chat_store(tmp_path, "epic-9", [gui_chat(messages=messages)])

    result = TraycerAnnotator().collect(tmp_path)

    assert {a.session_id for a in result.annotations} == {"session-gui", "session-earlier"}
    older = [a for a in result.annotations if a.session_id == "session-earlier"]
    assert ("workspace", "/repo/old") in {(a.key, a.value) for a in older}
    assert len([t for t in result.titles if t.session_id == "session-earlier"]) == 1


def test_a_chat_with_no_session_chain_binds_nothing(tmp_path):
    write_chat_store(tmp_path, "epic-9", [gui_chat(hostPrivate={"revision": 0, "data": {}})])

    assert TraycerAnnotator().collect(tmp_path).annotations == []


def test_terminal_agents_and_desktop_chats_are_both_read(tmp_path):
    write_chat_store(tmp_path, "epic-9", [chat(), gui_chat()])

    result = TraycerAnnotator().collect(tmp_path)

    assert {a.session_id for a in result.annotations} == {"session-9", "session-gui"}
    assert {a.value for a in result.annotations if a.key == "kind"} == {"agent", "chat"}
    assert result.groups[0]["sessions"] == "2"
