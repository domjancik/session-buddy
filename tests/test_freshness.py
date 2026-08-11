import os
import shutil
from pathlib import Path

from session_buddy.indexer import check_index_freshness, index_all


FIXTURES = Path(__file__).parent / "fixtures"


def copy_fixtures(tmp_path: Path) -> Path:
    workspace = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, workspace)
    return workspace


def test_freshness_detects_metadata_only_changes(tmp_path: Path) -> None:
    workspace = copy_fixtures(tmp_path)
    db_path = tmp_path / "index.sqlite"
    claude_home = workspace / "claude"
    codex_home = workspace / "codex"

    index_all(db_path, claude_home, codex_home, semantic_backend="hash")
    fresh = check_index_freshness(db_path, claude_home, codex_home)
    assert fresh.stale is False

    metadata_path = claude_home / "projects/test-project/sessions-index.json"
    bump_mtime(metadata_path)

    stale = check_index_freshness(db_path, claude_home, codex_home)
    assert stale.stale is True
    assert stale.changed == 1
    assert stale.changed_source == 0
    assert stale.changed_metadata == 1


def test_freshness_detects_deleted_sources(tmp_path: Path) -> None:
    workspace = copy_fixtures(tmp_path)
    db_path = tmp_path / "index.sqlite"
    claude_home = workspace / "claude"
    codex_home = workspace / "codex"

    index_all(db_path, claude_home, codex_home, semantic_backend="hash")
    source_path = claude_home / "projects/test-project/11111111-1111-1111-1111-111111111111.jsonl"
    source_path.unlink()

    stale = check_index_freshness(db_path, claude_home, codex_home)
    assert stale.stale is True
    assert stale.deleted == 1


def test_incremental_index_reindexes_after_metadata_change(tmp_path: Path) -> None:
    workspace = copy_fixtures(tmp_path)
    db_path = tmp_path / "index.sqlite"
    claude_home = workspace / "claude"
    codex_home = workspace / "codex"

    index_all(db_path, claude_home, codex_home, semantic_backend="hash")
    (codex_home / "session_index.jsonl").write_text(
        '{"id":"22222222-2222-2222-2222-222222222222","thread_name":"Renamed session search","updated_at":"2026-06-09T10:02:00.000Z"}\n',
        encoding="utf-8",
    )

    stale = check_index_freshness(db_path, claude_home, codex_home)
    assert stale.changed_metadata == 1

    stats = index_all(db_path, claude_home, codex_home, semantic_backend="hash")
    assert stats.indexed == 1
    assert check_index_freshness(db_path, claude_home, codex_home).stale is False


def bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
