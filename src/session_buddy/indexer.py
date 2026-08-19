from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .annotators import ANNOTATORS
from .database import IndexDatabase
from .embeddings import Embedder
from .parsers import (
    default_opencode_home,
    infer_codex_id_from_filename,
    load_claude_indexes,
    load_codex_session_index,
    load_codex_thread_metadata,
    opencode_db_path,
    opencode_metadata_fingerprint,
    opencode_stat_key,
    opencode_source_path,
    parse_claude_session,
    parse_codex_session,
    parse_opencode_session,
    safe_stat_key,
    scan_claude_sessions,
    scan_codex_sessions,
    scan_opencode_sessions,
)


@dataclass(slots=True)
class IndexStats:
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    pruned: int = 0
    total_seen: int = 0
    embed_backend: str = ""
    annotated: int = 0
    retitled: int = 0
    annotation_sources: list[str] = field(default_factory=list)
    annotation_warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FreshnessStats:
    new: int = 0
    changed: int = 0
    changed_source: int = 0
    changed_metadata: int = 0
    deleted: int = 0
    unchanged: int = 0
    total_seen: int = 0

    @property
    def stale(self) -> bool:
        return bool(self.new or self.changed or self.deleted)

    def summary(self) -> str:
        if not self.stale:
            return f"Index up to date ({self.unchanged} indexed sources checked)."
        return (
            "Index stale: "
            f"{self.new} new, {self.changed} changed "
            f"({self.changed_source} source, {self.changed_metadata} metadata), "
            f"{self.deleted} deleted."
        )


def index_all(
    db_path: Path,
    claude_home: Path,
    codex_home: Path,
    opencode_home: Path | None = None,
    force: bool = False,
    prune: bool = False,
    semantic: bool = True,
    semantic_backend: str = "auto",
    semantic_model: str = "BAAI/bge-small-en-v1.5",
) -> IndexStats:
    opencode_home = opencode_home or default_opencode_home()
    db = IndexDatabase(db_path)
    embedder = Embedder(semantic_backend, semantic_model) if semantic else None
    stats = IndexStats(embed_backend=embedder.backend if embedder else "disabled")
    seen_paths: set[str] = set()

    claude_entries, claude_project_paths = load_claude_indexes(claude_home)
    for path in scan_claude_sessions(claude_home):
        seen_paths.add(str(path))
        metadata_fingerprint = claude_metadata_fingerprint(path)
        if should_skip(db, path, force, metadata_fingerprint):
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
            record.metadata_fingerprint = metadata_fingerprint
            db.upsert_session(record, embedder)
            stats.indexed += 1
        except Exception:
            stats.failed += 1

    codex_threads = load_codex_thread_metadata(codex_home)
    codex_index = load_codex_session_index(codex_home)
    for path in scan_codex_sessions(codex_home):
        seen_paths.add(str(path))
        inferred_id = infer_codex_id_from_filename(path.name)
        metadata_fingerprint = codex_metadata_fingerprint(
            codex_threads.get(inferred_id),
            codex_index.get(inferred_id),
        )
        if should_skip(db, path, force, metadata_fingerprint):
            stats.skipped += 1
            continue
        try:
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
            record.metadata_fingerprint = metadata_fingerprint
            db.upsert_session(record, embedder)
            stats.indexed += 1
        except Exception:
            stats.failed += 1

    opencode_db = opencode_db_path(opencode_home)
    for row in scan_opencode_sessions(opencode_home):
        identity = Path(opencode_source_path(opencode_db, str(row.get("id") or "")))
        seen_paths.add(str(identity))
        metadata_fingerprint = opencode_metadata_fingerprint(row)
        if should_skip(db, identity, force, metadata_fingerprint, stat_key=opencode_stat_key(row)):
            stats.skipped += 1
            continue
        try:
            record = parse_opencode_session(opencode_db, row)
            if record is None:
                stats.failed += 1
                continue
            record.metadata_fingerprint = metadata_fingerprint
            db.upsert_session(record, embedder)
            stats.indexed += 1
        except Exception:
            stats.failed += 1

    stats.total_seen = len(seen_paths)
    stats.pruned = db.mark_seen_sources(seen_paths, prune=prune)
    run_annotators(db, stats)
    db.close()
    return stats


def run_annotators(db: IndexDatabase, stats: IndexStats, annotators=None) -> None:
    """Attach orchestrator metadata to sessions the providers already indexed.

    Runs last, because annotators key on (provider, session_id) and those rows must
    exist. Each annotator is isolated: one that raises costs only its own annotations.
    """
    titles = []
    for annotator in (ANNOTATORS if annotators is None else annotators):
        try:
            if not annotator.detect():
                continue
            result = annotator.collect()
        except Exception as error:
            stats.annotation_warnings.append(f"{annotator.source}: {type(error).__name__}: {error}")
            continue
        if result.warning:
            stats.annotation_warnings.append(result.warning)
        if not result.annotations and not result.titles:
            continue
        stats.annotated += db.replace_annotations(annotator.source, result.annotations)
        db.replace_groups(annotator.source, result.groups)
        stats.annotation_sources.append(annotator.source)
        titles.extend(result.titles)
    # Applied in one pass so priority resolution sees every claim at once, and so a
    # session whose claim disappeared reverts to its provider title.
    stats.retitled = db.apply_annotated_titles(titles)


def check_index_freshness(
    db_path: Path, claude_home: Path, codex_home: Path, opencode_home: Path | None = None
) -> FreshnessStats:
    db = IndexDatabase(db_path)
    try:
        return check_index_freshness_db(db, claude_home, codex_home, opencode_home)
    finally:
        db.close()


def check_index_freshness_db(
    db: IndexDatabase, claude_home: Path, codex_home: Path, opencode_home: Path | None = None
) -> FreshnessStats:
    opencode_home = opencode_home or default_opencode_home()
    stats = FreshnessStats()
    seen_paths: set[str] = set()

    for path in scan_claude_sessions(claude_home):
        seen_paths.add(str(path))
        update_freshness_for_path(stats, db, path, claude_metadata_fingerprint(path))

    codex_threads = load_codex_thread_metadata(codex_home)
    codex_index = load_codex_session_index(codex_home)
    for path in scan_codex_sessions(codex_home):
        seen_paths.add(str(path))
        inferred_id = infer_codex_id_from_filename(path.name)
        update_freshness_for_path(
            stats,
            db,
            path,
            codex_metadata_fingerprint(codex_threads.get(inferred_id), codex_index.get(inferred_id)),
        )

    opencode_db = opencode_db_path(opencode_home)
    for row in scan_opencode_sessions(opencode_home):
        identity = Path(opencode_source_path(opencode_db, str(row.get("id") or "")))
        seen_paths.add(str(identity))
        update_freshness_for_path(
            stats, db, identity, opencode_metadata_fingerprint(row), stat_key=opencode_stat_key(row)
        )

    for state in db.source_states():
        if state.source_path not in seen_paths:
            stats.deleted += 1

    stats.total_seen = len(seen_paths)
    return stats


def update_freshness_for_path(
    stats: FreshnessStats,
    db: IndexDatabase,
    path: Path,
    metadata_fingerprint: str,
    stat_key: tuple[int, int] | None = None,
) -> None:
    state = db.source_state(str(path))
    if state is None:
        stats.new += 1
        return
    mtime, size = stat_key if stat_key is not None else safe_stat_key(path)
    source_changed = state.file_mtime != mtime or state.file_size != size
    metadata_changed = state.metadata_fingerprint != metadata_fingerprint
    if source_changed or metadata_changed:
        stats.changed += 1
        if source_changed:
            stats.changed_source += 1
        if metadata_changed:
            stats.changed_metadata += 1
    else:
        stats.unchanged += 1


def should_skip(
    db: IndexDatabase,
    path: Path,
    force: bool,
    metadata_fingerprint: str,
    stat_key: tuple[int, int] | None = None,
) -> bool:
    if force:
        return False
    mtime, size = stat_key if stat_key is not None else safe_stat_key(path)
    state = db.source_state(str(path))
    return (
        state is not None
        and state.file_mtime == mtime
        and state.file_size == size
        and state.metadata_fingerprint == metadata_fingerprint
    )


def claude_metadata_fingerprint(session_path: Path) -> str:
    return stat_fingerprint([session_path.parent / "sessions-index.json"])


def codex_metadata_fingerprint(thread_meta: dict[str, Any] | None, index_meta: dict[str, Any] | None) -> str:
    thread_keys = [
        "id",
        "title",
        "cwd",
        "created_at_ms",
        "updated_at_ms",
        "git_branch",
        "preview",
    ]
    index_keys = ["id", "thread_name", "updated_at"]
    payload = {
        "thread": pick_keys(thread_meta, thread_keys),
        "index": pick_keys(index_meta, index_keys),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def pick_keys(value: dict[str, Any] | None, keys: list[str]) -> dict[str, Any]:
    if not value:
        return {}
    return {key: value.get(key) for key in keys if key in value}


def stat_fingerprint(paths: list[Path]) -> str:
    parts: list[str] = []
    for path in paths:
        if not path.exists():
            parts.append(f"{path}:missing")
            continue
        mtime, size = safe_stat_key(path)
        parts.append(f"{path}:{mtime}:{size}")
    return "|".join(parts)
