from __future__ import annotations

import json

import pytest

from session_buddy.annotators.traycer import TraycerAnnotator, first_workspace

pycrdt = pytest.importorskip("pycrdt", reason="traycer extra not installed")


def write_epic(root, epic_id, title, agents, *, updated_at=1700000000000):
    """Build a Yjs doc shaped like Traycer's epic record and persist it as a seed."""
    from pycrdt import Doc, Map

    doc = Doc()
    epic = doc.get("epic", type=Map)
    tui = Map()
    epic["id"] = epic_id
    epic["title"] = title
    epic["updatedAt"] = updated_at
    epic["tuiAgents"] = tui
    for agent in agents:
        tui[agent["id"]] = Map(agent)
    seeds = root / "epics" / epic_id / "seeds"
    seeds.mkdir(parents=True, exist_ok=True)
    (seeds / "epic.bin").write_bytes(doc.get_update())
    return seeds


AGENT = {
    "id": "agent-1",
    "harnessId": "claude",
    "harnessSessionId": "session-1",
    "title": "ACME-142 retry backoff gate",
    "parentId": "agent-parent",
    "workspaceFolders": json.dumps(["/repo/worktree-a", "/repo/worktree-b"]),
}


def test_detect_requires_an_epics_directory(tmp_path):
    assert TraycerAnnotator().detect(tmp_path) is False
    (tmp_path / "epics").mkdir()
    assert TraycerAnnotator().detect(tmp_path) is True


def test_collect_joins_agents_to_provider_sessions(tmp_path):
    write_epic(tmp_path, "epic-1", "Payments Integration Analysis", [AGENT])

    result = TraycerAnnotator().collect(tmp_path)

    pairs = {(a.key, a.value) for a in result.annotations}
    assert ("epic_title", "Payments Integration Analysis") in pairs
    assert ("agent_title", "ACME-142 retry backoff gate") in pairs
    assert ("parent_agent_id", "agent-parent") in pairs
    assert ("workspace", "/repo/worktree-a") in pairs
    assert {a.provider for a in result.annotations} == {"claude"}
    assert {a.session_id for a in result.annotations} == {"session-1"}
    assert {a.source for a in result.annotations} == {"traycer"}


def test_collect_claims_the_agent_title(tmp_path):
    write_epic(tmp_path, "epic-1", "Epic", [AGENT])

    titles = TraycerAnnotator().collect(tmp_path).titles

    assert [(t.provider, t.session_id, t.title) for t in titles] == [
        ("claude", "session-1", "ACME-142 retry backoff gate")
    ]


def test_agents_without_a_harness_session_are_skipped(tmp_path):
    orphan = {**AGENT, "id": "agent-2", "harnessSessionId": "", "title": "no session"}
    write_epic(tmp_path, "epic-1", "Epic", [orphan])

    result = TraycerAnnotator().collect(tmp_path)

    assert result.annotations == []
    assert result.titles == []


def test_groups_are_reported_for_listing(tmp_path):
    write_epic(tmp_path, "epic-1", "Alpha", [AGENT])

    groups = TraycerAnnotator().collect(tmp_path).groups

    assert groups == [{"id": "epic-1", "title": "Alpha", "updated_at": "1700000000000", "agents": "1"}]


def test_a_corrupt_seed_does_not_lose_the_other_epics(tmp_path):
    write_epic(tmp_path, "epic-good", "Good", [AGENT])
    bad = tmp_path / "epics" / "epic-bad" / "seeds"
    bad.mkdir(parents=True)
    (bad / "epic.bin").write_bytes(b"not a yjs update")

    result = TraycerAnnotator().collect(tmp_path)

    assert [g["title"] for g in result.groups] == ["Good"]
    assert "could not be decoded" in result.warning


def test_artifact_room_seeds_are_ignored(tmp_path):
    seeds = write_epic(tmp_path, "epic-1", "Alpha", [AGENT])
    (seeds / "artifact-room-epic-1-01ABC.bin").write_bytes(b"unrelated document")

    assert TraycerAnnotator().collect(tmp_path).groups[0]["title"] == "Alpha"


def test_missing_pycrdt_degrades_to_a_warning(tmp_path, monkeypatch):
    write_epic(tmp_path, "epic-1", "Alpha", [AGENT])
    monkeypatch.setitem(__import__("sys").modules, "pycrdt", None)

    result = TraycerAnnotator().collect(tmp_path)

    assert result.annotations == []
    assert "session-buddy[traycer]" in result.warning  # the install hint, not the decode failure


@pytest.mark.parametrize(
    "value,expected",
    [
        (json.dumps(["/a", "/b"]), "/a"),
        (["/a"], "/a"),
        ("", ""),
        (None, ""),
        ("/plain/path", "/plain/path"),
    ],
)
def test_first_workspace(value, expected):
    assert first_workspace(value) == expected
