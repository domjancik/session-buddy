from __future__ import annotations

import json
from pathlib import Path

from session_buddy.indexer import check_index_freshness_db, index_all
from session_buddy.database import IndexDatabase
from session_buddy.parsers import (
    infer_cwd_from_pi_dir,
    infer_pi_id_from_filename,
    parse_pi_session,
    scan_pi_sessions,
    strip_pi_injections,
)
from session_buddy.resume import build_resume_command

SESSION_ID = "01a02ebf-025a-7009-a71a-2cedec77cc72"
CWD = "/Users/dev/acme-mono"


def message(role, blocks, ts="2026-08-23T13:13:24.449Z", mid="m1", parent=None):
    return {
        "type": "message",
        "id": mid,
        "parentId": parent,
        "timestamp": ts,
        "message": {"role": role, "content": blocks},
    }


def write_session(root: Path, *, records=None, profile=None, session_id=SESSION_ID, cwd=CWD) -> Path:
    base = root / "agent" if profile is None else root / "profiles" / profile
    folder = base / "sessions" / "--Users-dev-acme-mono--"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"2026-08-23T13-11-05-050Z_{session_id}.jsonl"
    rows = [
        {"type": "session", "version": 3, "id": session_id,
         "timestamp": "2026-08-23T13:11:05.050Z", "cwd": cwd},
        {"type": "model_change", "model": "some-model"},
        {"type": "thinking_level_change", "level": "high"},
    ] + (records if records is not None else [
        message("user", [{"type": "text", "text": "why are webhooks not retried"}]),
        message("assistant", [
            {"type": "thinking", "thinking": "secret reasoning", "thinkingSignature": "blob"},
            {"type": "text", "text": "The drain filter reads live state."},
        ], mid="m2"),
        message("assistant", [{"type": "toolCall", "name": "bash", "args": {"cmd": "ls"}}], mid="m3"),
        message("toolResult", [{"type": "text", "text": "total 0"}], mid="m4"),
    ])
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_scan_finds_agent_and_profile_sessions(tmp_path):
    write_session(tmp_path)
    write_session(tmp_path, profile="bonsai", session_id="01a02ecb-efcb-7cff-87cf-bc735d3c57c3")

    found = scan_pi_sessions(tmp_path)

    assert len(found) == 2
    assert {p.parts[-4] for p in found} == {"agent", "bonsai"}


def test_scan_returns_nothing_without_a_pi_home(tmp_path):
    assert scan_pi_sessions(tmp_path / "absent") == []


def test_parse_reads_header_and_messages(tmp_path):
    record = parse_pi_session(write_session(tmp_path))

    assert record.provider == "pi"
    assert record.session_id == SESSION_ID
    assert record.cwd == CWD
    assert record.title == "why are webhooks not retried"
    assert [m.role for m in record.messages] == ["user", "assistant", "toolResult"]


def test_thinking_and_tool_calls_are_excluded(tmp_path):
    record = parse_pi_session(write_session(tmp_path))

    body = " ".join(m.text for m in record.messages)
    assert "secret reasoning" not in body
    assert "blob" not in body
    assert "The drain filter reads live state." in body
    assert "total 0" in body  # tool *output* is real content


def test_settings_events_are_not_messages(tmp_path):
    record = parse_pi_session(write_session(tmp_path))

    assert record.message_count == 3  # the toolCall-only message has no text


def test_skill_injection_is_stripped_but_the_prompt_survives(tmp_path):
    """pi prepends the whole SKILL.md to the user's turn; the prompt follows it."""
    injected = (
        '<skill name="session-buddy" location="/x/SKILL.md">\n# Session Buddy\n'
        "Use the locally installed `sb` command.\n</skill>\n\nwhere did I work with session buddy?"
    )
    record = parse_pi_session(
        write_session(tmp_path, records=[message("user", [{"type": "text", "text": injected}])])
    )

    assert record.title == "where did I work with session buddy?"
    assert "Session Buddy" not in record.messages[0].text
    assert record.messages[0].text == "where did I work with session buddy?"


def test_strip_injections_leaves_ordinary_text_alone():
    assert strip_pi_injections("plain question") == "plain question"
    assert strip_pi_injections("<skill a='1'>doc</skill>") == ""


def test_session_id_falls_back_to_the_filename():
    assert infer_pi_id_from_filename("2026-08-23T13-11-05-050Z_01a02ebf-025a.jsonl") == "01a02ebf-025a"
    assert infer_pi_id_from_filename("nounderscore.jsonl") == "nounderscore"


def test_cwd_decoding_is_only_a_fallback(tmp_path):
    assert infer_cwd_from_pi_dir("--Users-dev-acme--") == "/Users/dev/acme"
    # header cwd wins, because decoding cannot tell a separator from a literal dash
    record = parse_pi_session(write_session(tmp_path, cwd="/Users/dev/acme-mono"))
    assert record.cwd == "/Users/dev/acme-mono"


def test_resume_passes_the_session_path_not_the_id():
    command = build_resume_command("pi", SESSION_ID, CWD, "", "/home/.pi/agent/sessions/x/s.jsonl")

    assert command.argv == ["pi", "--session", "/home/.pi/agent/sessions/x/s.jsonl"]
    assert command.shell_line().startswith(f"cd {CWD} && ")


def test_resume_falls_back_to_the_id_without_a_path():
    assert build_resume_command("pi", SESSION_ID, CWD).argv == ["pi", "--session", SESSION_ID]


def test_index_and_freshness_round_trip(tmp_path):
    home = tmp_path / "pi"
    write_session(home)
    write_session(home, profile="bonsai", session_id="01a02ecb-efcb-7cff-87cf-bc735d3c57c3")
    index_path = tmp_path / "index.sqlite"
    empty = tmp_path / "none"

    stats = index_all(db_path=index_path, claude_home=empty, codex_home=empty,
                      opencode_home=empty, pi_home=home, semantic=False)
    assert stats.indexed == 2

    db = IndexDatabase(index_path)
    try:
        fresh = check_index_freshness_db(db, empty, empty, empty, home)
        assert fresh.stale is False
        assert fresh.unchanged == 2
    finally:
        db.close()


def test_a_corrupt_transcript_does_not_raise(tmp_path):
    path = write_session(tmp_path)
    path.write_text("not json\n{also not\n")

    assert parse_pi_session(path) is None
