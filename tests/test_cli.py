from __future__ import annotations

import argparse
import sys
from pathlib import Path

from session_search.cli import cmd_tui, current_launch_argv


def tui_args(**overrides: object) -> argparse.Namespace:
    values = {
        "db": Path("index.sqlite"),
        "query": "",
        "provider": None,
        "cwd": None,
        "auto_index": False,
        "no_tmux": False,
        "claude_home": Path(".claude"),
        "codex_home": Path(".codex"),
        "_session_search_argv": ["session-search", "tui"],
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_current_launch_argv_uses_module_invocation_for_injected_argv() -> None:
    assert current_launch_argv(["tui", "query"]) == [sys.executable, "-m", "session_search", "tui", "query"]


def test_current_launch_argv_rewrites_python_module_entrypoint(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["/repo/src/session_search/__main__.py", "tui"])

    assert current_launch_argv() == [sys.executable, "-m", "session_search", "tui"]


def test_cmd_tui_bootstraps_tmux_before_freshness_checks(monkeypatch) -> None:
    calls: list[tuple[list[str], str]] = []

    def fake_bootstrap(command_argv: list[str], *, cwd: str) -> int | None:
        calls.append((command_argv, cwd))
        return 9

    def fail_freshness(*args: object, **kwargs: object) -> str:
        raise AssertionError("freshness should run inside the bootstrapped tmux command")

    monkeypatch.setattr("session_search.cli.os.getcwd", lambda: "/repo")
    monkeypatch.setattr("session_search.cli.run_command_in_new_tmux_session_if_available", fake_bootstrap)
    monkeypatch.setattr("session_search.cli.handle_freshness_for_process", fail_freshness)

    assert cmd_tui(tui_args(_session_search_argv=["session-search", "tui", "fraud"])) == 9
    assert calls == [(["session-search", "tui", "fraud"], "/repo")]


def test_cmd_tui_runs_textual_when_tmux_bootstrap_is_unavailable(monkeypatch) -> None:
    calls: list[tuple[Path, str, str]] = []

    monkeypatch.setattr("session_search.cli.run_command_in_new_tmux_session_if_available", lambda *a, **k: None)
    monkeypatch.setattr("session_search.cli.handle_freshness_for_process", lambda *a, **k: "Index is fresh.")

    def fake_run_tui(db_path: Path, query: str, **kwargs: object) -> None:
        calls.append((db_path, query, str(kwargs["index_status"])))

    monkeypatch.setattr("session_search.cli.run_tui", fake_run_tui)

    assert cmd_tui(tui_args(query="chargeback")) == 0
    assert calls == [(Path("index.sqlite"), "chargeback", "Index is fresh.")]


def test_cmd_tui_no_tmux_skips_bootstrap(monkeypatch) -> None:
    bootstrapped = False

    def fake_bootstrap(*args: object, **kwargs: object) -> int | None:
        nonlocal bootstrapped
        bootstrapped = True
        return 0

    monkeypatch.setattr("session_search.cli.run_command_in_new_tmux_session_if_available", fake_bootstrap)
    monkeypatch.setattr("session_search.cli.handle_freshness_for_process", lambda *a, **k: "Index is fresh.")
    monkeypatch.setattr("session_search.cli.run_tui", lambda *a, **k: None)

    assert cmd_tui(tui_args(no_tmux=True)) == 0
    assert bootstrapped is False
