from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from .resume import PreparedResumeCommand


@dataclass(slots=True)
class TmuxPaneCommand:
    argv: list[str]

    def shell_line(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)


def resolve_tmux_executable() -> str | None:
    return shutil.which("tmux")


def inside_tmux(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get("TMUX"))


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
