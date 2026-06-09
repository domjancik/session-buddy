from pathlib import Path

import pytest

from session_search.database import IndexDatabase
from session_search.embeddings import Embedder
from session_search.models import MessageRecord, SessionRecord
from session_search.resume import (
    PreparedResumeCommand,
    build_resume_command,
    prepare_resume_command,
    resolve_agent_executable,
    run_prepared_resume,
)
from session_search.search import search_sessions


def test_search_finds_indexed_session(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite"
    db = IndexDatabase(db_path)
    record = SessionRecord(
        provider="codex",
        session_id="s1",
        title="Investigate webhook retry",
        cwd="/Users/example/dev/acme-mono",
        created_at=1781000000000,
        updated_at=1781000000000,
        git_branch="main",
        source_path="/tmp/s1.jsonl",
        file_mtime=1,
        file_size=1,
        message_count=1,
        first_prompt="Investigate webhook retry failure",
        summary="",
        preview="The session discusses inbound webhook retry routing.",
        messages=[
            MessageRecord(
                provider="codex",
                session_id="s1",
                idx=0,
                role="user",
                timestamp=1781000000000,
                text="Investigate inbound webhook retry routing failure",
            )
        ],
    )
    db.upsert_session(record, Embedder("hash"))
    db.close()

    results = search_sessions(db_path, "inbound enrichment", limit=5)

    assert results
    assert results[0].session_id == "s1"
    assert results[0].cwd == "/Users/example/dev/acme-mono"


def test_resume_commands() -> None:
    codex = build_resume_command("codex", "abc", "/repo")
    claude = build_resume_command("claude", "def", "/repo")

    assert codex.argv == ["codex", "resume", "-C", "/repo", "abc"]
    assert claude.argv == ["claude", "--resume", "def"]
    assert "cd /repo" in claude.shell_line()


def test_prepare_resume_falls_back_when_indexed_cwd_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    command = build_resume_command("codex", "abc", str(missing))

    prepared = prepare_resume_command(command, fallback_cwd=str(fallback), resolve_executable=False)

    assert prepared.cwd == str(fallback)
    assert prepared.argv == ["codex", "resume", "-C", str(fallback), "abc"]
    assert "Indexed cwd no longer exists" in prepared.warnings[0]


@pytest.mark.parametrize("provider,version_arg", [("claude", "--version"), ("codex", "--version")])
def test_real_agent_cli_is_executable(provider: str, version_arg: str) -> None:
    executable = resolve_agent_executable(provider)
    assert executable is not None, f"{provider} CLI should be available"

    command = PreparedResumeCommand(
        provider=provider,
        session_id="version-check",
        cwd="",
        argv=[executable, version_arg],
        warnings=[],
    )

    assert run_prepared_resume(command) == 0
