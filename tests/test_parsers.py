from pathlib import Path

from session_search.parsers import (
    load_claude_indexes,
    load_codex_session_index,
    parse_claude_session,
    parse_codex_session,
)


ROOT = Path(__file__).parent / "fixtures"


def test_parse_claude_session_uses_index_metadata() -> None:
    claude_home = ROOT / "claude"
    entries, projects = load_claude_indexes(claude_home)
    path = claude_home / "projects/test-project/11111111-1111-1111-1111-111111111111.jsonl"

    record = parse_claude_session(path, entries[str(path)], projects[str(path.parent)])

    assert record is not None
    assert record.provider == "claude"
    assert record.session_id == "11111111-1111-1111-1111-111111111111"
    assert record.cwd == "/Users/example/dev/acme-mono"
    assert record.git_branch == "main"
    assert "webhook retry" in record.first_prompt.lower()
    assert len(record.messages) == 2


def test_parse_codex_session_reads_meta_and_messages() -> None:
    codex_home = ROOT / "codex"
    index = load_codex_session_index(codex_home)
    path = codex_home / "sessions/2026/06/09/rollout-2026-06-09T10-00-00-22222222-2222-2222-2222-222222222222.jsonl"

    record = parse_codex_session(path, None, index["22222222-2222-2222-2222-222222222222"])

    assert record is not None
    assert record.provider == "codex"
    assert record.session_id == "22222222-2222-2222-2222-222222222222"
    assert record.cwd == "/Users/example/dev/session-search"
    assert record.git_branch == "feature/search"
    assert record.title == "Build session search"
    assert len(record.messages) == 2
