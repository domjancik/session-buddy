from __future__ import annotations

import subprocess

import pytest

from session_buddy.resume import PreparedResumeCommand
from session_buddy.tmux import (
    TMUX_BOOTSTRAP_ENV,
    build_tmux_session_command,
    build_tmux_pane_command,
    inside_tmux,
    open_tmux_pane,
    prepare_tmux_pane_command,
    run_command_in_new_tmux_session_if_available,
    should_bootstrap_tmux,
    tmux_bootstrap_guarded,
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


def test_tmux_bootstrap_guard_checks_environment() -> None:
    assert tmux_bootstrap_guarded({}) is False
    assert tmux_bootstrap_guarded({TMUX_BOOTSTRAP_ENV: "1"}) is True


def test_should_bootstrap_tmux_requires_tmux_outside_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_buddy.tmux.resolve_tmux_executable", lambda: "/usr/bin/tmux")

    assert should_bootstrap_tmux({}) is True
    assert should_bootstrap_tmux({"TMUX": "inside"}) is False
    assert should_bootstrap_tmux({TMUX_BOOTSTRAP_ENV: "1"}) is False


def test_should_bootstrap_tmux_skips_when_tmux_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_buddy.tmux.resolve_tmux_executable", lambda: None)

    assert should_bootstrap_tmux({}) is False


def test_build_tmux_session_command_sets_guard_and_cwd() -> None:
    command = build_tmux_session_command(
        ["session-buddy", "tui", "checkout bug"],
        cwd="/repo with spaces",
        session_name="session-buddy-test",
        tmux_executable="/usr/bin/tmux",
    )

    assert command.argv == [
        "/usr/bin/tmux",
        "new-session",
        "-s",
        "session-buddy-test",
        "-n",
        "search",
        "-c",
        "/repo with spaces",
        "-e",
        f"{TMUX_BOOTSTRAP_ENV}=1",
        "session-buddy",
        "tui",
        "checkout bug",
    ]
    assert command.shell_line().startswith("/usr/bin/tmux new-session -s session-buddy-test")


def test_run_command_in_new_tmux_session_if_available_skips_without_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_buddy.tmux.resolve_tmux_executable", lambda: None)

    assert run_command_in_new_tmux_session_if_available(["session-buddy", "tui"], cwd="/repo") is None


def test_run_command_in_new_tmux_session_if_available_skips_inside_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_buddy.tmux.resolve_tmux_executable", lambda: "/usr/bin/tmux")
    monkeypatch.setattr("session_buddy.tmux.inside_tmux", lambda: True)

    assert run_command_in_new_tmux_session_if_available(["session-buddy", "tui"], cwd="/repo") is None


def test_run_command_in_new_tmux_session_if_available_runs_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("session_buddy.tmux.resolve_tmux_executable", lambda: "/usr/bin/tmux")
    monkeypatch.setattr("session_buddy.tmux.inside_tmux", lambda: False)
    monkeypatch.setattr("session_buddy.tmux.tmux_bootstrap_guarded", lambda: False)
    monkeypatch.setattr("session_buddy.tmux.os.getpid", lambda: 12345)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 7, "", "")

    monkeypatch.setattr("session_buddy.tmux.subprocess.run", fake_run)

    assert run_command_in_new_tmux_session_if_available(["session-buddy", "tui"], cwd="/repo") == 7
    assert calls == [
        [
            "/usr/bin/tmux",
            "new-session",
            "-s",
            "session-buddy-12345",
            "-n",
            "search",
            "-c",
            "/repo",
            "-e",
            f"{TMUX_BOOTSTRAP_ENV}=1",
            "session-buddy",
            "tui",
        ]
    ]


def test_prepare_tmux_pane_requires_tmux_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_buddy.tmux.resolve_tmux_executable", lambda: None)

    with pytest.raises(RuntimeError, match="Could not find executable: tmux"):
        prepare_tmux_pane_command(prepared_command())


def test_prepare_tmux_pane_requires_tmux_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_buddy.tmux.resolve_tmux_executable", lambda: "/usr/bin/tmux")
    monkeypatch.setattr("session_buddy.tmux.inside_tmux", lambda: False)

    with pytest.raises(RuntimeError, match="requires running session-buddy inside tmux"):
        prepare_tmux_pane_command(prepared_command())


def test_open_tmux_pane_runs_split_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("session_buddy.tmux.resolve_tmux_executable", lambda: "/usr/bin/tmux")
    monkeypatch.setattr("session_buddy.tmux.inside_tmux", lambda: True)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("session_buddy.tmux.subprocess.run", fake_run)

    pane = open_tmux_pane(prepared_command())

    assert calls == [pane.argv]
    assert pane.argv[0] == "/usr/bin/tmux"


def test_open_tmux_pane_reports_tmux_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("session_buddy.tmux.resolve_tmux_executable", lambda: "/usr/bin/tmux")
    monkeypatch.setattr("session_buddy.tmux.inside_tmux", lambda: True)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "no current client")

    monkeypatch.setattr("session_buddy.tmux.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="no current client"):
        open_tmux_pane(prepared_command())
