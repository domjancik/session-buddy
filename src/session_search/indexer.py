from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .database import IndexDatabase
from .embeddings import Embedder
from .parsers import (
    infer_codex_id_from_filename,
    load_claude_indexes,
    load_codex_session_index,
    load_codex_thread_metadata,
    parse_claude_session,
    parse_codex_session,
    safe_stat_key,
    scan_claude_sessions,
    scan_codex_sessions,
)


@dataclass(slots=True)
class IndexStats:
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    pruned: int = 0
    total_seen: int = 0
    embed_backend: str = ""


def index_all(
    db_path: Path,
    claude_home: Path,
    codex_home: Path,
    force: bool = False,
    prune: bool = False,
    semantic: bool = True,
    semantic_backend: str = "auto",
    semantic_model: str = "BAAI/bge-small-en-v1.5",
) -> IndexStats:
    db = IndexDatabase(db_path)
    embedder = Embedder(semantic_backend, semantic_model) if semantic else None
    stats = IndexStats(embed_backend=embedder.backend if embedder else "disabled")
    seen_paths: set[str] = set()

    claude_entries, claude_project_paths = load_claude_indexes(claude_home)
    for path in scan_claude_sessions(claude_home):
        seen_paths.add(str(path))
        if should_skip(db, path, force):
            stats.skipped += 1
            continue
        try:
            record = parse_claude_session(
                path,
                claude_entries.get(str(path)),
                claude_project_paths.get(str(path.parent)),
            )
            if record is None:
                stats.failed += 1
                continue
            db.upsert_session(record, embedder)
            stats.indexed += 1
        except Exception:
            stats.failed += 1

    codex_threads = load_codex_thread_metadata(codex_home)
    codex_index = load_codex_session_index(codex_home)
    for path in scan_codex_sessions(codex_home):
        seen_paths.add(str(path))
        if should_skip(db, path, force):
            stats.skipped += 1
            continue
        try:
            inferred_id = infer_codex_id_from_filename(path.name)
            record = parse_codex_session(
                path,
                codex_threads.get(inferred_id),
                codex_index.get(inferred_id),
            )
            if record is None:
                stats.failed += 1
                continue
            if record.session_id in codex_threads and not record.cwd:
                record.cwd = str(codex_threads[record.session_id].get("cwd") or "")
            db.upsert_session(record, embedder)
            stats.indexed += 1
        except Exception:
            stats.failed += 1

    stats.total_seen = len(seen_paths)
    stats.pruned = db.mark_seen_sources(seen_paths, prune=prune)
    db.close()
    return stats


def should_skip(db: IndexDatabase, path: Path, force: bool) -> bool:
    if force:
        return False
    mtime, size = safe_stat_key(path)
    state = db.source_state(str(path))
    return state is not None and state.file_mtime == mtime and state.file_size == size
