from __future__ import annotations

import subprocess

import pytest

from session_search.resume import PreparedResumeCommand
from session_search.tmux import (
    build_tmux_pane_command,
    inside_tmux,
    open_tmux_pane,
    prepare_tmux_pane_command,
)


def prepared_command() -> PreparedResumeCommand:
    return PreparedResumeCommand(
        provider="claude",
        session_id="session-1",
        cwd="/repo with spaces",
        argv=["/opt/homebrew/bin/claude", "--resume", "session-1"],
        warnings=[],
    )


def test_build_tmux_pane_command_uses_direct_argv_and_original_cwd() -> None:
    pane = build_tmux_pane_command(prepared_command(), tmux_executable="/usr/bin/tmux")

    assert pane.argv == [
        "/usr/bin/tmux",
        "split-window",
        "-h",
        "-c",
        "/repo with spaces",
        "/opt/homebrew/bin/claude",
        "--resume",
        "session-1",
    ]
    assert pane.shell_line().startswith("/usr/bin/tmux split-window -h -c '/repo with spaces'")


def test_build_tmux_pane_command_supports_down_split() -> None:
    pane = build_tmux_pane_command(prepared_command(), split="down")

    assert pane.argv[2] == "-v"


def test_build_tmux_pane_command_rejects_unknown_split() -> None:
    with pytest.raises(ValueError, match="Unsupported tmux split direction"):
        build_tmux_pane_command(prepared_command(), split="diagonal")


def test_inside_tmux_checks_environment() -> None:
    assert inside_tmux({}) is False
    assert inside_tmux({"TMUX": "/tmp/tmux-501/default,123,0"}) is True


def test_prepare_tmux_pane_requires_tmux_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_search.tmux.resolve_tmux_executable", lambda: None)

    with pytest.raises(RuntimeError, match="Could not find executable: tmux"):
        prepare_tmux_pane_command(prepared_command())


def test_prepare_tmux_pane_requires_tmux_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_search.tmux.resolve_tmux_executable", lambda: "/usr/bin/tmux")
    monkeypatch.setattr("session_search.tmux.inside_tmux", lambda: False)

    with pytest.raises(RuntimeError, match="requires running session-search inside tmux"):
        prepare_tmux_pane_command(prepared_command())


def test_open_tmux_pane_runs_split_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("session_search.tmux.resolve_tmux_executable", lambda: "/usr/bin/tmux")
    monkeypatch.setattr("session_search.tmux.inside_tmux", lambda: True)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("session_search.tmux.subprocess.run", fake_run)

    pane = open_tmux_pane(prepared_command())

    assert calls == [pane.argv]
    assert pane.argv[0] == "/usr/bin/tmux"


def test_open_tmux_pane_reports_tmux_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_search.tmux.resolve_tmux_executable", lambda: "/usr/bin/tmux")
    monkeypatch.setattr("session_search.tmux.inside_tmux", lambda: True)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "no current client")

    monkeypatch.setattr("session_search.tmux.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="no current client"):
        open_tmux_pane(prepared_command())
