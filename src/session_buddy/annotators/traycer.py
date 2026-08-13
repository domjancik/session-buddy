"""Traycer annotator.

Traycer groups agent sessions into *epics*. Its store is `~/.traycer/epics/<id>/seeds/*.bin`,
Yjs update binaries whose root map is the epic record defined in traycerai/traycer at
`protocol/src/persistence/_internal/epic-schemas.ts`:

    { id, title, isTitleEditedByUser, createdAt, updatedAt,
      chats, artifacts, deletedArtifacts, tuiAgents, roleClaims }

`tuiAgents[]` is the join to us: each carries `harnessId` (our provider) and
`harnessSessionId` (our session_id), plus a human `title` that is far better than the
first-prompt fallback we derive for orchestrated sessions, whose opening message is
usually machine chatter.

`desktop-windows.json` also holds epic names, but only for tabs that happen to be open,
so it is not a usable source.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import AnnotatedTitle, Annotation
from .base import AnnotatorResult

SOURCE = "traycer"
TITLE_PRIORITY = 10


class TraycerAnnotator:
    source = SOURCE
    title_priority = TITLE_PRIORITY

    def default_home(self) -> Path:
        return Path.home() / ".traycer"

    def detect(self, home: Path | None = None) -> bool:
        root = home or self.default_home()
        return (root / "epics").is_dir()

    def collect(self, home: Path | None = None) -> AnnotatorResult:
        root = home or self.default_home()
        result = AnnotatorResult()
        epics_dir = root / "epics"
        if not epics_dir.is_dir():
            return result

        try:
            from pycrdt import Doc, Map
        except ImportError:
            result.warning = (
                "traycer annotations skipped: install the extra with "
                "`uv tool install 'session-buddy[traycer]'`"
            )
            return result

        failed = 0
        for epic_dir in sorted(epics_dir.iterdir()):
            if not epic_dir.is_dir():
                continue
            try:
                record = self.read_epic(epic_dir, Doc, Map)
            except Exception:
                # A single unreadable seed must not cost us the other epics.
                failed += 1
                continue
            if record is None:
                continue
            result.groups.append(
                {
                    "id": record["id"],
                    "title": record["title"],
                    "updated_at": str(record["updated_at"]),
                    "agents": str(len(record["agents"])),
                }
            )
            for agent in record["agents"]:
                provider = agent.get("harnessId") or ""
                session_id = agent.get("harnessSessionId") or ""
                if not provider or not session_id:
                    continue
                pairs = {
                    "epic_id": record["id"],
                    "epic_title": record["title"],
                    "agent_id": agent.get("id", ""),
                    "agent_title": agent.get("title", ""),
                    "parent_agent_id": agent.get("parentId", "") or "",
                    "workspace": first_workspace(agent.get("workspaceFolders")),
                }
                for key, value in pairs.items():
                    if value:
                        result.annotations.append(
                            Annotation(provider, session_id, SOURCE, key, str(value))
                        )
                title = (agent.get("title") or "").strip()
                if title:
                    result.titles.append(
                        AnnotatedTitle(provider, session_id, SOURCE, title, TITLE_PRIORITY)
                    )
        if failed:
            result.warning = f"traycer: {failed} epic seed(s) could not be decoded"
        return result

    def read_epic(self, epic_dir: Path, doc_cls, map_cls) -> dict | None:
        seeds = epic_dir / "seeds"
        if not seeds.is_dir():
            return None
        for seed in sorted(seeds.glob("*.bin")):
            # Artifact rooms are separate documents; only the epic doc has the root map.
            if seed.name.startswith("artifact-room-"):
                continue
            doc = doc_cls()
            doc.apply_update(seed.read_bytes())
            epic = doc.get("epic", type=map_cls)
            keys = set(epic.keys())
            if "title" not in keys:
                continue
            agents = []
            if "tuiAgents" in keys:
                tui = epic["tuiAgents"]
                for agent_key in list(tui.keys()):
                    rec = tui[agent_key]
                    agents.append({k: rec[k] for k in rec.keys()})
            return {
                "id": str(epic["id"]) if "id" in keys else epic_dir.name,
                "title": str(epic["title"] or ""),
                "updated_at": int(epic["updatedAt"]) if "updatedAt" in keys and epic["updatedAt"] else 0,
                "agents": agents,
            }
        return None


def first_workspace(value) -> str:
    """tuiAgents.workspaceFolders is a JSON-encoded list; keep the first path."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        value = parsed
    if isinstance(value, list) and value:
        return str(value[0])
    return ""
