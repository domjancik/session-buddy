from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .database import IndexDatabase


@dataclass(slots=True)
class ResumeCommand:
    provider: str
    session_id: str
    cwd: str
    argv: list[str]
    git_branch: str = ""

    def shell_line(self) -> str:
        prefix = f"cd {shlex.quote(self.cwd)} && " if self.cwd else ""
        return prefix + " ".join(shlex.quote(part) for part in self.argv)


@dataclass(slots=True)
class PreparedResumeCommand:
    provider: str
    session_id: str
    cwd: str
    argv: list[str]
    warnings: list[str]
    indexed_cwd_missing: bool = False
    restore_worktree_command: str | None = None

    def shell_line(self) -> str:
        prefix = f"cd {shlex.quote(self.cwd)} && " if self.cwd else ""
        return prefix + " ".join(shlex.quote(part) for part in self.argv)


def build_resume_command(provider: str, session_id: str, cwd: str, git_branch: str = "") -> ResumeCommand:
    if provider == "codex":
        argv = ["codex", "resume"]
        if cwd:
            argv.extend(["-C", cwd])
        argv.append(session_id)
    elif provider == "claude":
        argv = ["claude", "--resume", session_id]
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    return ResumeCommand(provider, session_id, cwd, argv, git_branch)


def load_resume_command(db_path: Path, provider: str, session_id: str, cwd_override: str | None = None) -> ResumeCommand:
    db = IndexDatabase(db_path)
    try:
        row = db.get_session(provider, session_id)
        if row is None:
            raise KeyError(f"No indexed {provider} session with id {session_id}")
        cwd = cwd_override if cwd_override is not None else str(row["cwd"] or "")
        return build_resume_command(provider, session_id, cwd, str(row["git_branch"] or ""))
    finally:
        db.close()


def resolve_agent_executable(name: str) -> str | None:
    resolved = shutil.which(name)
    if resolved:
        return resolved

    home = Path.home()
    candidates = [
        home / ".local/bin" / name,
        home / ".bun/bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
        Path("/usr/bin") / name,
    ]
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def prepare_resume_command(
    command: ResumeCommand,
    fallback_cwd: str | None = None,
    resolve_executable: bool = True,
) -> PreparedResumeCommand:
    warnings: list[str] = []
    argv = list(command.argv)
    cwd = command.cwd
    indexed_cwd_missing = False
    restore_command: str | None = None

    if cwd and not Path(cwd).is_dir():
        indexed_cwd_missing = True
        restore_command = build_restore_worktree_command(cwd, command.git_branch)
        if restore_command:
            warnings.append(f"Indexed cwd no longer exists: {cwd}. Restore worktree with: {restore_command}")
        else:
            warnings.append(f"Indexed cwd no longer exists: {cwd}")
        if command.provider == "claude":
            warnings.append("Claude resume is project-directory scoped; restore the original cwd before resuming.")
        else:
            fallback = fallback_cwd or os.getcwd()
            warnings.append(f"Resuming from fallback cwd: {fallback}")
            cwd = fallback
            if command.provider == "codex":
                argv = replace_codex_cwd_arg(argv, fallback)

    if resolve_executable:
        executable = resolve_agent_executable(argv[0])
        if executable is None:
            raise RuntimeError(f"Could not find executable: {argv[0]}")
        argv[0] = executable

    return PreparedResumeCommand(
        provider=command.provider,
        session_id=command.session_id,
        cwd=cwd,
        argv=argv,
        warnings=warnings,
        indexed_cwd_missing=indexed_cwd_missing,
        restore_worktree_command=restore_command,
    )


def build_restore_worktree_command(cwd: str, git_branch: str = "") -> str | None:
    path = Path(cwd)
    if path.parent.name != ".worktrees":
        return None
    repo = path.parent.parent
    if not repo.is_dir():
        return None
    branch = git_branch or path.name
    if not branch:
        return None
    return " ".join(
        [
            "git",
            "-C",
            shlex.quote(str(repo)),
            "worktree",
            "add",
            shlex.quote(str(path)),
            shlex.quote(branch),
        ]
    )


def replace_codex_cwd_arg(argv: list[str], cwd: str) -> list[str]:
    updated = list(argv)
    try:
        index = updated.index("-C")
    except ValueError:
        try:
            index = updated.index("--cd")
        except ValueError:
            return updated
    if index + 1 < len(updated):
        updated[index + 1] = cwd
    return updated


def run_resume(command: ResumeCommand, replace_process: bool = False) -> int:
    prepared = prepare_resume_command(command)
    for warning in prepared.warnings:
        print(warning, file=sys.stderr)
    if prepared.provider == "claude" and prepared.indexed_cwd_missing:
        raise RuntimeError("Cannot resume Claude session until the original cwd is restored.")
    return run_prepared_resume(prepared, replace_process=replace_process)


def run_prepared_resume(command: PreparedResumeCommand, replace_process: bool = False) -> int:
    cwd = command.cwd or None
    if replace_process:
        if cwd:
            os.chdir(cwd)
        os.execvp(command.argv[0], command.argv)
        return 0
    try:
        return subprocess.run(command.argv, cwd=cwd).returncode
    except FileNotFoundError as error:
        if cwd and not Path(cwd).is_dir():
            raise RuntimeError(f"Resume cwd does not exist: {cwd}") from None
        raise RuntimeError(f"Could not execute {command.argv[0]}: {error}") from None
