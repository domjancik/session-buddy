from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .indexer import check_index_freshness, index_all
from .resume import load_resume_command, prepare_resume_command, run_resume
from .search import format_result_line, search_sessions
from .text import format_time
from .tui import run_tui


DEFAULT_DB = Path(".session-search/index.sqlite")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args) or 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session-search")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Index database path.")
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
    search_parser.add_argument("--provider", choices=["claude", "codex"])
    search_parser.add_argument("--cwd", help="Only show sessions whose cwd contains this text.")
    search_parser.add_argument("--no-semantic", action="store_true")
    search_parser.add_argument("--auto-index", action="store_true", help="Update stale index sources before searching.")
    add_source_args(search_parser)
    search_parser.set_defaults(func=cmd_search)

    tui_parser = subparsers.add_parser("tui", help="Open the terminal search UI.")
    tui_parser.add_argument("query", nargs="?", default="")
    tui_parser.add_argument("--provider", choices=["claude", "codex"])
    tui_parser.add_argument("--cwd", help="Only show sessions whose cwd contains this text.")
    tui_parser.add_argument("--auto-index", action="store_true", help="Update stale index sources before opening the TUI.")
    add_source_args(tui_parser)
    tui_parser.set_defaults(func=cmd_tui)

    status_parser = subparsers.add_parser("status", help="Report whether the index is stale.")
    add_source_args(status_parser)
    status_parser.set_defaults(func=cmd_status)

    resume_parser = subparsers.add_parser("resume", help="Resume an indexed session.")
    resume_parser.add_argument("provider", choices=["claude", "codex"])
    resume_parser.add_argument("session_id")
    resume_parser.add_argument("--cwd", help="Override indexed cwd.")
    resume_parser.add_argument("--print-command", action="store_true", help="Print the command instead of running it.")
    resume_parser.add_argument("--exec", action="store_true", help="Replace this process with Claude/Codex.")
    resume_parser.set_defaults(func=cmd_resume)
    return parser


def add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--claude-home", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")


def cmd_index(args: argparse.Namespace) -> int:
    stats = index_all(
        db_path=args.db,
        claude_home=args.claude_home.expanduser(),
        codex_home=args.codex_home.expanduser(),
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
    return 1 if stats.failed else 0


def cmd_search(args: argparse.Namespace) -> int:
    handle_freshness_for_process(args, auto_index=args.auto_index, semantic=not args.no_semantic)
    results = search_sessions(
        args.db,
        args.query,
        limit=args.limit,
        provider=args.provider,
        cwd=args.cwd,
        semantic=not args.no_semantic,
    )
    for index, result in enumerate(results, start=1):
        print(format_result_line(index, result))
        print(f"    cwd: {result.cwd}")
        print(f"    id: {result.session_id}")
        if result.git_branch:
            print(f"    branch: {result.git_branch}")
        if result.updated_at:
            print(f"    updated: {format_time(result.updated_at)} UTC")
        for snippet in result.snippets[:2]:
            print(f"    match: {snippet}")
        print()
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    freshness_message = handle_freshness_for_process(args, auto_index=args.auto_index, semantic=True)
    run_tui(args.db, args.query, provider=args.provider, cwd_filter=args.cwd, index_status=freshness_message)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    freshness = check_index_freshness(
        args.db,
        args.claude_home.expanduser(),
        args.codex_home.expanduser(),
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
    if auto_index:
        stats = index_all(
            db_path=args.db,
            claude_home=claude_home,
            codex_home=codex_home,
            prune=True,
            semantic=semantic,
            semantic_backend="auto",
        )
        return (
            f"Auto-indexed {stats.indexed}, skipped {stats.skipped}, "
            f"failed {stats.failed}, pruned {stats.pruned}."
        )

    freshness = check_index_freshness(args.db, claude_home, codex_home)
    message = freshness.summary()
    if freshness.stale:
        print(f"{message} Run `session-search index` or pass `--auto-index`.", file=sys.stderr)
    return message


def cmd_resume(args: argparse.Namespace) -> int:
    try:
        command = load_resume_command(args.db, args.provider, args.session_id, cwd_override=args.cwd)
    except KeyError as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.print_command:
        try:
            prepared = prepare_resume_command(command)
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            return 1
        for warning in prepared.warnings:
            print(warning, file=sys.stderr)
        print(prepared.shell_line())
        return 0
    try:
        return run_resume(command, replace_process=args.exec)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
