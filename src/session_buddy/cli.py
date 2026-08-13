from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .database import IndexDatabase
from .indexer import check_index_freshness, index_all
from .parsers import default_opencode_home
from .resume import load_resume_command, prepare_resume_command, run_resume
from .search import format_result_line, search_sessions
from .text import format_time
from .tmux import build_tmux_pane_command, open_tmux_pane, run_command_in_new_tmux_session_if_available
from .tui import run_tui


DEFAULT_DB = Path.home() / ".session-buddy" / "index.sqlite"

SUBCOMMANDS = ("index", "search", "tui", "status", "resume", "groups")


def default_db_path() -> Path:
    override = os.environ.get("SESSION_BUDDY_DB")
    return Path(override).expanduser() if override else DEFAULT_DB


def expanded_path(value: str) -> Path:
    return Path(value).expanduser()


def program_name() -> str:
    name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    return name if name and name != "__main__.py" else "session-buddy"


GLOBAL_FLAGS_WITH_VALUE = ("--db",)
GLOBAL_FLAGS = ("--auto-index",)


def insert_default_subcommand(argv: list[str]) -> list[str]:
    """Treat a bare query as `search <query>` so the common case needs no subcommand."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in SUBCOMMANDS or token in ("-h", "--help"):
            return argv
        if token in GLOBAL_FLAGS_WITH_VALUE:
            index += 2
            continue
        if token in GLOBAL_FLAGS or any(token.startswith(f"{flag}=") for flag in GLOBAL_FLAGS_WITH_VALUE):
            index += 1
            continue
        if token.startswith("-"):
            return argv
        return [*argv[:index], "search", *argv[index:]]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(insert_default_subcommand(list(argv if argv is not None else sys.argv[1:])))
    args._session_buddy_argv = current_launch_argv(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args) or 0)


def current_launch_argv(argv: list[str] | None = None) -> list[str]:
    if argv is not None:
        return [sys.executable, "-m", "session_buddy", *argv]
    current = list(sys.argv)
    if current and Path(current[0]).name == "__main__.py":
        return [sys.executable, "-m", "session_buddy", *current[1:]]
    return current


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=program_name())
    parser.add_argument(
        "--db",
        type=expanded_path,
        default=default_db_path(),
        help="Index database path. Defaults to $SESSION_BUDDY_DB or ~/.session-buddy/index.sqlite.",
    )
    parser.add_argument(
        "--auto-index",
        dest="global_auto_index",
        action="store_true",
        help="Update stale sources first. Accepted before the subcommand so it can live in a shell alias.",
    )
    subparsers = parser.add_subparsers(dest="command")

    index_parser = subparsers.add_parser("index", help="Index Claude and Codex sessions.")
    add_source_args(index_parser)
    index_parser.add_argument("--force", action="store_true", help="Re-index unchanged files.")
    index_parser.add_argument("--prune", action="store_true", help="Remove sessions whose source files disappeared.")
    index_parser.add_argument("--no-semantic", action="store_true", help="Do not compute embeddings.")
    index_parser.add_argument("--semantic-backend", default="auto", choices=["auto", "hash", "sentence-transformers"])
    index_parser.add_argument("--semantic-model", default="BAAI/bge-small-en-v1.5")
    index_parser.set_defaults(func=cmd_index)

    search_parser = subparsers.add_parser("search", help="Search indexed sessions.")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--provider", choices=["claude", "codex", "opencode"])
    search_parser.add_argument("--cwd", help="Only show sessions whose cwd contains this text.")
    search_parser.add_argument("--no-semantic", action="store_true")
    search_parser.add_argument("--auto-index", action="store_true", help="Update stale index sources before searching.")
    search_parser.add_argument(
        "--ext",
        action="append",
        metavar="SOURCE[.KEY][=VALUE]",
        help="Filter on annotator metadata, e.g. --ext traycer.epic_title=Payments. Repeatable.",
    )
    search_parser.add_argument(
        "--show-ext", action="store_true", help="Print annotator metadata under each result."
    )
    add_source_args(search_parser)
    search_parser.set_defaults(func=cmd_search)

    tui_parser = subparsers.add_parser("tui", help="Open the terminal search UI.")
    tui_parser.add_argument("query", nargs="?", default="")
    tui_parser.add_argument("--provider", choices=["claude", "codex", "opencode"])
    tui_parser.add_argument("--cwd", help="Only show sessions whose cwd contains this text.")
    tui_parser.add_argument("--auto-index", action="store_true", help="Update stale index sources before opening the TUI.")
    tui_parser.add_argument("--no-tmux", action="store_true", help="Do not auto-start the TUI inside tmux.")
    add_source_args(tui_parser)
    tui_parser.set_defaults(func=cmd_tui)

    groups_parser = subparsers.add_parser(
        "groups", help="List annotator groups (Traycer epics, and whatever later tools add)."
    )
    groups_parser.add_argument("--source", help="Limit to one annotator, e.g. traycer.")
    groups_parser.set_defaults(func=cmd_groups)

    status_parser = subparsers.add_parser("status", help="Report whether the index is stale.")
    add_source_args(status_parser)
    status_parser.set_defaults(func=cmd_status)

    resume_parser = subparsers.add_parser("resume", help="Resume an indexed session.")
    resume_parser.add_argument(
        "provider",
        nargs="?",
        choices=["claude", "codex", "opencode"],
        help="Optional. Inferred from the index when the session id is unambiguous.",
    )
    resume_parser.add_argument("session_id")
    resume_parser.add_argument("--cwd", help="Override indexed cwd.")
    resume_parser.add_argument("--print-command", action="store_true", help="Print the command instead of running it.")
    resume_parser.add_argument("--exec", action="store_true", help="Replace this process with Claude/Codex.")
    resume_parser.add_argument("--tmux-pane", action="store_true", help="Open the resume command in a tmux split pane.")
    resume_parser.add_argument(
        "--tmux-split",
        choices=["right", "down"],
        default="right",
        help="Direction for --tmux-pane. Defaults to right.",
    )
    resume_parser.set_defaults(func=cmd_resume)
    return parser


def add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--claude-home", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--opencode-home", type=Path, default=default_opencode_home())


def cmd_index(args: argparse.Namespace) -> int:
    stats = index_all(
        db_path=args.db,
        claude_home=args.claude_home.expanduser(),
        codex_home=args.codex_home.expanduser(),
        opencode_home=args.opencode_home.expanduser(),
        force=args.force,
        prune=args.prune,
        semantic=not args.no_semantic,
        semantic_backend=args.semantic_backend,
        semantic_model=args.semantic_model,
    )
    print(f"Indexed: {stats.indexed}")
    print(f"Skipped: {stats.skipped}")
    print(f"Failed: {stats.failed}")
    print(f"Pruned: {stats.pruned}")
    print(f"Seen: {stats.total_seen}")
    print(f"Semantic backend: {stats.embed_backend}")
    if stats.annotation_sources:
        print(f"Annotations: {stats.annotated} from {', '.join(stats.annotation_sources)}")
        print(f"Retitled: {stats.retitled}")
    for warning in stats.annotation_warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    return 1 if stats.failed else 0


def wants_auto_index(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "auto_index", False) or getattr(args, "global_auto_index", False))


def cmd_search(args: argparse.Namespace) -> int:
    handle_freshness_for_process(args, auto_index=wants_auto_index(args), semantic=not args.no_semantic)
    results = search_sessions(
        args.db,
        args.query,
        limit=args.limit,
        provider=args.provider,
        cwd=args.cwd,
        semantic=not args.no_semantic,
        ext=args.ext,
    )
    for index, result in enumerate(results, start=1):
        print(format_result_line(index, result))
        print(f"    cwd: {result.cwd}")
        print(f"    id: {result.session_id}")
        if result.git_branch:
            print(f"    branch: {result.git_branch}")
        if result.updated_at:
            print(f"    updated: {format_time(result.updated_at)} UTC")
        if getattr(args, "show_ext", False):
            db = IndexDatabase(args.db)
            try:
                for source, pairs in db.annotations_for(result.provider, result.session_id).items():
                    rendered = " ".join(f"{k}={v}" for k, v in pairs.items())
                    print(f"    {source}: {rendered}")
            finally:
                db.close()
        for snippet in result.snippets[:2]:
            print(f"    match: {snippet}")
        print()
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    if not args.no_tmux:
        returncode = run_command_in_new_tmux_session_if_available(args._session_buddy_argv, cwd=os.getcwd())
        if returncode is not None:
            return returncode

    freshness_message = handle_freshness_for_process(args, auto_index=wants_auto_index(args), semantic=True)
    run_tui(args.db, args.query, provider=args.provider, cwd_filter=args.cwd, index_status=freshness_message)
    return 0


def cmd_groups(args: argparse.Namespace) -> int:
    from .annotators import ANNOTATORS

    rows = []
    for annotator in ANNOTATORS:
        if args.source and annotator.source != args.source:
            continue
        if not annotator.detect():
            continue
        result = annotator.collect()
        if result.warning:
            print(f"Warning: {result.warning}", file=sys.stderr)
        for group in result.groups:
            rows.append((annotator.source, group))
    if not rows:
        print("No annotator groups found.", file=sys.stderr)
        return 1
    rows.sort(key=lambda r: -int(r[1].get("updated_at") or 0))
    for source, group in rows:
        agents = group.get("agents", "0")
        print(f"{source}  {group.get('id', '')[:8]}  {agents:>3} agents  {group.get('title') or '(untitled)'}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    freshness = check_index_freshness(
        args.db,
        args.claude_home.expanduser(),
        args.codex_home.expanduser(),
        args.opencode_home.expanduser(),
    )
    print(freshness.summary())
    print(f"New: {freshness.new}")
    print(f"Changed: {freshness.changed}")
    print(f"Changed source: {freshness.changed_source}")
    print(f"Changed metadata: {freshness.changed_metadata}")
    print(f"Deleted: {freshness.deleted}")
    print(f"Unchanged: {freshness.unchanged}")
    print(f"Seen: {freshness.total_seen}")
    return 1 if freshness.stale else 0


def handle_freshness_for_process(args: argparse.Namespace, auto_index: bool, semantic: bool) -> str:
    claude_home = args.claude_home.expanduser()
    codex_home = args.codex_home.expanduser()
    opencode_home = args.opencode_home.expanduser()
    if auto_index:
        stats = index_all(
            db_path=args.db,
            claude_home=claude_home,
            codex_home=codex_home,
            opencode_home=opencode_home,
            prune=True,
            semantic=semantic,
            semantic_backend="auto",
        )
        return (
            f"Auto-indexed {stats.indexed}, skipped {stats.skipped}, "
            f"failed {stats.failed}, pruned {stats.pruned}."
        )

    freshness = check_index_freshness(args.db, claude_home, codex_home, opencode_home)
    message = freshness.summary()
    if freshness.stale:
        print(f"{message} Run `{program_name()} index` or pass `--auto-index`.", file=sys.stderr)
    return message


def cmd_resume(args: argparse.Namespace) -> int:
    if args.exec and args.tmux_pane:
        print("--exec cannot be used with --tmux-pane", file=sys.stderr)
        return 2

    providers = [args.provider] if args.provider else ["claude", "codex", "opencode"]
    command = None
    errors: list[str] = []
    for provider in providers:
        try:
            command = load_resume_command(args.db, provider, args.session_id, cwd_override=args.cwd)
            break
        except KeyError as error:
            errors.append(str(error))
    if command is None:
        if args.provider:
            print(errors[-1], file=sys.stderr)
        else:
            print(f"No indexed session with id {args.session_id}", file=sys.stderr)
        return 1
    if args.print_command or args.tmux_pane:
        try:
            prepared = prepare_resume_command(command)
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            return 1
        for warning in prepared.warnings:
            print(warning, file=sys.stderr)
        if args.print_command:
            if args.tmux_pane:
                print(build_tmux_pane_command(prepared, split=args.tmux_split).shell_line())
            else:
                print(prepared.shell_line())
            return 0
        if prepared.provider == "claude" and prepared.indexed_cwd_missing:
            print("Cannot resume Claude session until the original cwd is restored.", file=sys.stderr)
            return 1
        try:
            open_tmux_pane(prepared, split=args.tmux_split)
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    try:
        return run_resume(command, replace_process=args.exec)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
