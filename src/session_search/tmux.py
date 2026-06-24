from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from .resume import PreparedResumeCommand


TMUX_BOOTSTRAP_ENV = "SESSION_SEARCH_TMUX_BOOTSTRAPPED"


@dataclass(slots=True)
class TmuxPaneCommand:
    argv: list[str]

    def shell_line(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)


@dataclass(slots=True)
class TmuxSessionCommand:
    argv: list[str]

    def shell_line(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)


def resolve_tmux_executable() -> str | None:
    return shutil.which("tmux")


def inside_tmux(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get("TMUX"))


def tmux_bootstrap_guarded(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get(TMUX_BOOTSTRAP_ENV))


def should_bootstrap_tmux(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return not inside_tmux(env) and not tmux_bootstrap_guarded(env) and resolve_tmux_executable() is not None


def build_tmux_session_command(
    command_argv: list[str],
    *,
    cwd: str,
    session_name: str,
    tmux_executable: str = "tmux",
) -> TmuxSessionCommand:
    argv = [
        tmux_executable,
        "new-session",
        "-s",
        session_name,
        "-n",
        "search",
        "-c",
        cwd,
        "-e",
        f"{TMUX_BOOTSTRAP_ENV}=1",
    ]
    argv.extend(command_argv)
    return TmuxSessionCommand(argv)


def run_command_in_new_tmux_session_if_available(command_argv: list[str], *, cwd: str) -> int | None:
    tmux = resolve_tmux_executable()
    if tmux is None or inside_tmux() or tmux_bootstrap_guarded():
        return None
    command = build_tmux_session_command(
        command_argv,
        cwd=cwd,
        session_name=f"session-search-{os.getpid()}",
        tmux_executable=tmux,
    )
    return subprocess.run(command.argv, check=False).returncode


def build_tmux_pane_command(
    command: PreparedResumeCommand,
    *,
    split: str = "right",
    tmux_executable: str = "tmux",
) -> TmuxPaneCommand:
    try:
        split_flag = {"right": "-h", "down": "-v"}[split]
    except KeyError:
        raise ValueError(f"Unsupported tmux split direction: {split}") from None

    argv = [tmux_executable, "split-window", split_flag]
    if command.cwd:
        argv.extend(["-c", command.cwd])
    argv.extend(command.argv)
    return TmuxPaneCommand(argv)


def prepare_tmux_pane_command(command: PreparedResumeCommand, *, split: str = "right") -> TmuxPaneCommand:
    tmux = resolve_tmux_executable()
    if tmux is None:
        raise RuntimeError("Could not find executable: tmux. Install tmux or use external resume.")
    if not inside_tmux():
        raise RuntimeError("Tmux pane resume requires running session-search inside tmux. Use Ctrl-O external resume or start tmux first.")
    return build_tmux_pane_command(command, split=split, tmux_executable=tmux)


def open_tmux_pane(command: PreparedResumeCommand, *, split: str = "right") -> TmuxPaneCommand:
    pane_command = prepare_tmux_pane_command(command, split=split)
    completed = subprocess.run(
        pane_command.argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if not detail:
            detail = f"tmux split-window exited with code {completed.returncode}"
        raise RuntimeError(f"Could not open tmux pane: {detail}")
    return pane_command
