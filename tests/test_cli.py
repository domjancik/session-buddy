from __future__ import annotations

import argparse
import sys
from pathlib import Path

from session_buddy.cli import (
    build_parser,
    cmd_tui,
    current_launch_argv,
    default_db_path,
    insert_default_subcommand,
    wants_auto_index,
)


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
        "_session_buddy_argv": ["session-buddy", "tui"],
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_current_launch_argv_uses_module_invocation_for_injected_argv() -> None:
    assert current_launch_argv(["tui", "query"]) == [sys.executable, "-m", "session_buddy", "tui", "query"]


def test_current_launch_argv_rewrites_python_module_entrypoint(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["/repo/src/session_buddy/__main__.py", "tui"])

    assert current_launch_argv() == [sys.executable, "-m", "session_buddy", "tui"]


def test_cmd_tui_bootstraps_tmux_before_freshness_checks(monkeypatch) -> None:
    calls: list[tuple[list[str], str]] = []

    def fake_bootstrap(command_argv: list[str], *, cwd: str) -> int | None:
        calls.append((command_argv, cwd))
        return 9

    def fail_freshness(*args: object, **kwargs: object) -> str:
        raise AssertionError("freshness should run inside the bootstrapped tmux command")

    monkeypatch.setattr("session_buddy.cli.os.getcwd", lambda: "/repo")
    monkeypatch.setattr("session_buddy.cli.run_command_in_new_tmux_session_if_available", fake_bootstrap)
    monkeypatch.setattr("session_buddy.cli.handle_freshness_for_process", fail_freshness)

    assert cmd_tui(tui_args(_session_buddy_argv=["session-buddy", "tui", "fraud"])) == 9
    assert calls == [(["session-buddy", "tui", "fraud"], "/repo")]


def test_cmd_tui_runs_textual_when_tmux_bootstrap_is_unavailable(monkeypatch) -> None:
    calls: list[tuple[Path, str, str]] = []

    monkeypatch.setattr("session_buddy.cli.run_command_in_new_tmux_session_if_available", lambda *a, **k: None)
    monkeypatch.setattr("session_buddy.cli.handle_freshness_for_process", lambda *a, **k: "Index is fresh.")

    def fake_run_tui(db_path: Path, query: str, **kwargs: object) -> None:
        calls.append((db_path, query, str(kwargs["index_status"])))

    monkeypatch.setattr("session_buddy.cli.run_tui", fake_run_tui)

    assert cmd_tui(tui_args(query="chargeback")) == 0
    assert calls == [(Path("index.sqlite"), "chargeback", "Index is fresh.")]


def test_cmd_tui_no_tmux_skips_bootstrap(monkeypatch) -> None:
    bootstrapped = False

    def fake_bootstrap(*args: object, **kwargs: object) -> int | None:
        nonlocal bootstrapped
        bootstrapped = True
        return 0

    monkeypatch.setattr("session_buddy.cli.run_command_in_new_tmux_session_if_available", fake_bootstrap)
    monkeypatch.setattr("session_buddy.cli.handle_freshness_for_process", lambda *a, **k: "Index is fresh.")
    monkeypatch.setattr("session_buddy.cli.run_tui", lambda *a, **k: None)

    assert cmd_tui(tui_args(no_tmux=True)) == 0
    assert bootstrapped is False


def test_bare_query_defaults_to_search() -> None:
    assert insert_default_subcommand(["webhook retry"]) == ["search", "webhook retry"]


def test_bare_query_keeps_global_db_flag_in_front() -> None:
    assert insert_default_subcommand(["--db", "/tmp/i.sqlite", "gate"]) == [
        "--db",
        "/tmp/i.sqlite",
        "search",
        "gate",
    ]
    assert insert_default_subcommand(["--db=/tmp/i.sqlite", "gate"]) == [
        "--db=/tmp/i.sqlite",
        "search",
        "gate",
    ]


def test_explicit_subcommand_is_left_alone() -> None:
    assert insert_default_subcommand(["status"]) == ["status"]
    assert insert_default_subcommand(["search", "gate"]) == ["search", "gate"]
    assert insert_default_subcommand(["resume", "abc"]) == ["resume", "abc"]


def test_help_and_flags_are_not_rewritten() -> None:
    assert insert_default_subcommand(["--help"]) == ["--help"]
    assert insert_default_subcommand([]) == []
    assert insert_default_subcommand(["--limit", "5"]) == ["--limit", "5"]


def test_default_db_is_absolute_and_env_overridable(monkeypatch) -> None:
    monkeypatch.delenv("SESSION_BUDDY_DB", raising=False)
    assert default_db_path().is_absolute()

    monkeypatch.setenv("SESSION_BUDDY_DB", "~/custom/index.sqlite")
    assert default_db_path() == Path.home() / "custom" / "index.sqlite"


def test_db_flag_expands_user() -> None:
    parser = build_parser()
    args = parser.parse_args(["--db", "~/somewhere/index.sqlite", "status"])

    assert args.db == Path.home() / "somewhere" / "index.sqlite"


def test_resume_provider_is_optional() -> None:
    parser = build_parser()

    inferred = parser.parse_args(["resume", "abc-123"])
    assert inferred.provider is None
    assert inferred.session_id == "abc-123"

    explicit = parser.parse_args(["resume", "codex", "abc-123"])
    assert explicit.provider == "codex"
    assert explicit.session_id == "abc-123"


def test_global_auto_index_flag_precedes_a_bare_query() -> None:
    assert insert_default_subcommand(["--auto-index", "gate"]) == ["--auto-index", "search", "gate"]


def test_global_auto_index_parses_before_subcommand() -> None:
    parser = build_parser()
    args = parser.parse_args(insert_default_subcommand(["--auto-index", "gate"]))

    assert args.global_auto_index is True
    assert args.auto_index is False
    assert args.query == "gate"
    assert wants_auto_index(args) is True


def test_subcommand_auto_index_still_works() -> None:
    parser = build_parser()
    args = parser.parse_args(["search", "gate", "--auto-index"])

    assert wants_auto_index(args) is True


def test_auto_index_defaults_off() -> None:
    parser = build_parser()

    assert wants_auto_index(parser.parse_args(["search", "gate"])) is False
