from __future__ import annotations

import sqlite3
from pathlib import Path

from session_buddy.annotators.conductor import ConductorAnnotator, to_epoch_ms

SCHEMA = """
create table sessions (
    id text primary key, status text, claude_session_id text, title text,
    workspace_id text, agent_type text, updated_at text
);
create table workspaces (
    id text primary key, repository_id text, directory_name text, workspace_name text,
    branch text, workspace_path text, updated_at text
);
create table repos (id text primary key, name text, root_path text);
"""


def make_home(tmp_path: Path, sessions=None, workspaces=None, repos=None) -> Path:
    home = tmp_path / "conductor"
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "conductor.db")
    conn.executescript(SCHEMA)
    conn.executemany(
        "insert into sessions (id, claude_session_id, title, workspace_id, agent_type, updated_at)"
        " values (?, ?, ?, ?, ?, ?)",
        sessions if sessions is not None else
        [("c1", "sess-1", "Review repo", "w1", "claude", "2026-08-24 14:06:47")],
    )
    conn.executemany(
        "insert into workspaces (id, repository_id, directory_name, workspace_name, branch,"
        " workspace_path, updated_at) values (?, ?, ?, ?, ?, ?, ?)",
        workspaces if workspaces is not None else
        [("w1", "r1", "puebla", "", "feature-branch", "/ws/puebla", "2026-08-24T14:06:29.474Z")],
    )
    conn.executemany(
        "insert into repos (id, name, root_path) values (?, ?, ?)",
        repos if repos is not None else [("r1", "acme-mono", "/dev/acme-mono")],
    )
    conn.commit()
    conn.close()
    return home


def test_detect_requires_the_database(tmp_path):
    assert ConductorAnnotator().detect(tmp_path) is False
    assert ConductorAnnotator().detect(make_home(tmp_path)) is True


def test_sessions_join_on_the_harness_id(tmp_path):
    result = ConductorAnnotator().collect(make_home(tmp_path))

    pairs = {(a.key, a.value) for a in result.annotations}
    assert ("workspace", "acme-mono/puebla") in pairs
    assert ("branch", "feature-branch") in pairs
    assert ("repo", "acme-mono") in pairs
    assert ("session_title", "Review repo") in pairs
    assert {a.provider for a in result.annotations} == {"claude"}
    assert {a.session_id for a in result.annotations} == {"sess-1"}


def test_agent_type_selects_the_provider(tmp_path):
    """claude_session_id holds whichever harness ran; agent_type says which one."""
    home = make_home(
        tmp_path,
        sessions=[
            ("c1", "sess-claude", "A", "w1", "claude", "2026-08-24 14:00:00"),
            ("c2", "sess-codex", "B", "w1", "codex", "2026-08-24 14:00:00"),
        ],
    )

    titles = ConductorAnnotator().collect(home).titles

    assert {(t.provider, t.session_id) for t in titles} == {
        ("claude", "sess-claude"),
        ("codex", "sess-codex"),
    }


def test_the_session_title_is_claimed(tmp_path):
    titles = ConductorAnnotator().collect(make_home(tmp_path)).titles

    assert [(t.session_id, t.title) for t in titles] == [("sess-1", "Review repo")]


def test_placeholder_titles_are_not_claimed(tmp_path):
    home = make_home(
        tmp_path,
        sessions=[
            ("c1", "sess-1", "Untitled", "w1", "claude", "2026-08-24 14:00:00"),
            ("c2", "sess-2", "   ", "w1", "claude", "2026-08-24 14:00:00"),
        ],
    )

    result = ConductorAnnotator().collect(home)

    assert result.titles == []
    assert ("session_title", "Untitled") not in {(a.key, a.value) for a in result.annotations}


def test_sessions_without_a_harness_id_are_skipped(tmp_path):
    home = make_home(
        tmp_path,
        sessions=[("c1", None, "Review repo", "w1", "claude", "2026-08-24 14:00:00")],
    )

    assert ConductorAnnotator().collect(home).annotations == []


def test_workspaces_become_groups(tmp_path):
    groups = ConductorAnnotator().collect(make_home(tmp_path)).groups

    assert groups == [
        {
            "id": "w1",
            "title": "acme-mono/puebla",
            "updated_at": str(to_epoch_ms("2026-08-24T14:06:29.474Z")),
            "key": "workspace_id",
        }
    ]


def test_workspace_name_wins_over_the_directory(tmp_path):
    home = make_home(
        tmp_path,
        workspaces=[("w1", "r1", "puebla", "Nice Name", "b", "/ws", "2026-08-24 14:00:00")],
    )

    assert ConductorAnnotator().collect(home).groups[0]["title"] == "acme-mono/Nice Name"


def test_a_workspace_without_a_repo_still_labels(tmp_path):
    home = make_home(
        tmp_path,
        workspaces=[("w1", "missing", "puebla", "", "b", "/ws", "2026-08-24 14:00:00")],
    )

    assert ConductorAnnotator().collect(home).groups[0]["title"] == "puebla"


def test_both_timestamp_formats_parse():
    assert to_epoch_ms("2026-08-24T14:06:29.474Z") == 1787580389474
    assert to_epoch_ms("2026-08-24 14:06:47") == 1787580407000
    assert to_epoch_ms("") == 0
    assert to_epoch_ms("not a date") == 0


def test_a_corrupt_database_degrades_to_a_warning(tmp_path):
    home = tmp_path / "conductor"
    home.mkdir()
    (home / "conductor.db").write_bytes(b"not a database")

    result = ConductorAnnotator().collect(home)

    assert result.annotations == []
    assert "conductor" in result.warning
