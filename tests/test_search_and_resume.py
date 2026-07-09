import shlex
import subprocess
from pathlib import Path

import pytest

from session_search.database import IndexDatabase
from session_search.embeddings import Embedder
from session_search.models import MessageRecord, SessionRecord
from session_search.resume import (
    PreparedResumeCommand,
    build_restore_worktree_argv,
    build_restore_worktree_command,
    build_resume_command,
    prepare_resume_command,
    resolve_agent_executable,
    run_prepared_resume,
    run_restore_worktree,
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
    repo = tmp_path / "repo"
    worktrees = repo / ".worktrees"
    worktrees.mkdir(parents=True)
    missing = worktrees / "feature with spaces"
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    command = build_resume_command("codex", "abc", str(missing), "feature/branch")

    prepared = prepare_resume_command(command, fallback_cwd=str(fallback), resolve_executable=False)

    assert prepared.cwd == str(fallback)
    assert prepared.argv == ["codex", "resume", "-C", str(fallback), "abc"]
    assert prepared.indexed_cwd_missing is True
    assert prepared.restore_worktree_command is not None
    assert prepared.restore_worktree_argv == [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        str(missing),
        "feature/branch",
    ]
    assert shlex.split(prepared.restore_worktree_command) == [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        str(missing),
        "feature/branch",
    ]
    assert "Restore worktree with:" in prepared.warnings[0]
    assert prepared.warnings[-1] == f"Resuming from fallback cwd: {fallback}"


def test_prepare_resume_keeps_missing_claude_cwd_and_offers_restore(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktrees = repo / ".worktrees"
    worktrees.mkdir(parents=True)
    missing = worktrees / "84-teal"
    command = build_resume_command("claude", "def", str(missing), "84-teal")

    prepared = prepare_resume_command(command, resolve_executable=False)

    assert prepared.cwd == str(missing)
    assert prepared.argv == ["claude", "--resume", "def"]
    assert prepared.indexed_cwd_missing is True
    assert prepared.restore_worktree_command is not None
    assert prepared.restore_worktree_argv == [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        str(missing),
        "84-teal",
    ]
    assert shlex.split(prepared.restore_worktree_command) == [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        str(missing),
        "84-teal",
    ]
    assert "Claude resume is project-directory scoped" in prepared.warnings[-1]


def test_build_restore_worktree_command_uses_worktree_name_when_branch_unknown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktrees = repo / ".worktrees"
    worktrees.mkdir(parents=True)
    missing = worktrees / "84-teal"

    command = build_restore_worktree_command(str(missing))

    assert command is not None
    assert shlex.split(command)[-1] == "84-teal"


def test_build_restore_worktree_argv_preserves_spaces_without_shell_quoting(tmp_path: Path) -> None:
    repo = tmp_path / "repo with spaces"
    worktrees = repo / ".worktrees"
    worktrees.mkdir(parents=True)
    missing = worktrees / "feature with spaces"

    argv = build_restore_worktree_argv(str(missing), "feature/with spaces")

    assert argv == [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        str(missing),
        "feature/with spaces",
    ]


def test_build_restore_worktree_command_ignores_non_worktree_paths(tmp_path: Path) -> None:
    missing = tmp_path / "repo" / "nested"

    assert build_restore_worktree_command(str(missing), "branch") is None


def test_run_restore_worktree_runs_git_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktrees = repo / ".worktrees"
    worktrees.mkdir(parents=True)
    missing = worktrees / "84-teal"
    command = build_resume_command("claude", "def", str(missing), "84-teal")
    prepared = prepare_resume_command(command, resolve_executable=False)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        Path(argv[5]).mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0, "restored", "")

    monkeypatch.setattr("session_search.resume.subprocess.run", fake_run)

    assert run_restore_worktree(prepared) == "restored"
    assert calls == [prepared.restore_worktree_argv]


def test_run_restore_worktree_reports_git_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktrees = repo / ".worktrees"
    worktrees.mkdir(parents=True)
    missing = worktrees / "84-teal"
    command = build_resume_command("claude", "def", str(missing), "84-teal")
    prepared = prepare_resume_command(command, resolve_executable=False)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 128, "", "branch is already checked out")

    monkeypatch.setattr("session_search.resume.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="branch is already checked out"):
        run_restore_worktree(prepared)


def test_run_restore_worktree_requires_restore_command() -> None:
    prepared = PreparedResumeCommand(
        provider="claude",
        session_id="def",
        cwd="/tmp/nope",
        argv=["claude", "--resume", "def"],
        warnings=[],
        indexed_cwd_missing=True,
    )

    with pytest.raises(RuntimeError, match="No restore worktree command"):
        run_restore_worktree(prepared)


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
