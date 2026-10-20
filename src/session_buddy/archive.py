"""A copy of an expired session, outside the index.

When a transcript expires the index becomes the only record of what was said. That file is
one SQLite database on one disk, with no snapshot behind it, so a session whose provider copy
is gone has a single point of failure. Writing it out as JSONL at the moment it is flagged
gives it a second home in a format that `rg` can read without this tool.
"""

from __future__ import annotations

import json
from pathlib import Path


def default_archive_dir(db_path: Path) -> Path:
    return db_path.parent / "archive"


def archive_path(root: Path, provider: str, session_id: str) -> Path:
    # Subagent ids carry a slash ("<parent>/agent-abc"), which would otherwise make a
    # directory and leave the parent's file and its children fighting over one name.
    return root / provider / f"{session_id.replace('/', '__')}.jsonl"


def write_session(root: Path, provider: str, session_id: str, title: str, source_path: str,
                  messages: list) -> Path:
    path = archive_path(root, provider, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "kind": "session",
        "provider": provider,
        "session_id": session_id,
        "title": title,
        "source_path": source_path,
    }
    lines = [json.dumps(header)]
    for message in messages:
        lines.append(json.dumps({
            "kind": "message",
            "idx": message["idx"],
            "role": message["role"],
            "timestamp": message["timestamp"],
            "text": message["text"],
        }))
    # One atomic replace: a half-written archive of an unrecoverable session is worse than
    # none, because the next run sees a file and leaves it alone.
    tmp = path.with_suffix(".jsonl.part")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_session(root: Path, provider: str, session_id: str) -> list[dict] | None:
    path = archive_path(root, provider, session_id)
    if not path.exists():
        return None
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in records if r.get("kind") == "message"]


class ArchivedSession:
    def __init__(self, provider: str, session_id: str, title: str, messages: list[dict]):
        self.provider = provider
        self.session_id = session_id
        self.title = title
        self.messages = messages


def read_archived_session(root: Path, session_id: str, provider: str | None = None):
    """Find an archived session by the short id a listing prints."""
    if not root.exists():
        return None
    providers = [provider] if provider else sorted(p.name for p in root.iterdir() if p.is_dir())
    for name in providers:
        directory = root / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            if not path.stem.replace("__", "/").startswith(session_id):
                continue
            records = [json.loads(line) for line in
                       path.read_text(encoding="utf-8").splitlines() if line]
            header = next((r for r in records if r.get("kind") == "session"), {})
            messages = [r for r in records if r.get("kind") == "message"]
            return ArchivedSession(header.get("provider", name),
                                   header.get("session_id", path.stem),
                                   header.get("title", ""), messages)
    return None
