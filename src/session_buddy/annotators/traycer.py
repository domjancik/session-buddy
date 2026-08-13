"""Traycer annotator.

Traycer groups agent sessions into *epics*. Its store is `~/.traycer/epics/<id>/seeds/*.bin`,
Yjs update binaries whose root map is the epic record defined in traycerai/traycer at
`protocol/src/persistence/_internal/epic-schemas.ts`:

    { id, title, isTitleEditedByUser, createdAt, updatedAt,
      chats, artifacts, deletedArtifacts, tuiAgents, roleClaims }

Traycer binds harness sessions in two places, and both must be read:

- `tuiAgents[]` - terminal agents. Each carries `harnessId` (our provider) and
  `harnessSessionId` (our session_id).
- `chats[]` - GUI chats. The binding lives in `activeSessionChain.{harnessId, sessionId}`.

Reading only `tuiAgents` misses every GUI chat: an epic driven from the desktop app has
`tuiAgents: []` and all of its work under `chats`.

Both carry a human `title`, far better than the first-prompt fallback we derive for
orchestrated sessions, whose opening message is usually machine chatter.

`activeSessionChain` names the *live* session only. A chat that was forked or resumed may
have earlier sessions that it no longer references, so those stay un-annotated.

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
                    "agents": str(len(record["bindings"])),
                }
            )
            for binding in record["bindings"]:
                provider = binding["provider"]
                session_id = binding["session_id"]
                if not provider or not session_id:
                    continue
                pairs = {
                    "epic_id": record["id"],
                    "epic_title": record["title"],
                    "kind": binding["kind"],
                    f"{binding['kind']}_id": binding["id"],
                    f"{binding['kind']}_title": binding["title"],
                    "parent_id": binding["parent_id"],
                    "workspace": binding["workspace"],
                }
                for key, value in pairs.items():
                    if value:
                        result.annotations.append(
                            Annotation(provider, session_id, SOURCE, key, str(value))
                        )
                if binding["title"]:
                    result.titles.append(
                        AnnotatedTitle(provider, session_id, SOURCE, binding["title"], TITLE_PRIORITY)
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
            bindings = []
            if "tuiAgents" in keys:
                bindings.extend(read_tui_agents(epic["tuiAgents"]))
            if "chats" in keys:
                bindings.extend(read_chats(epic["chats"]))
            return {
                "id": str(epic["id"]) if "id" in keys else epic_dir.name,
                "title": str(epic["title"] or ""),
                "updated_at": int(epic["updatedAt"]) if "updatedAt" in keys and epic["updatedAt"] else 0,
                "bindings": bindings,
            }
        return None


def read_tui_agents(tui) -> list[dict]:
    bindings = []
    for key in list(tui.keys()):
        rec = tui[key]
        fields = {k: rec[k] for k in rec.keys()}
        bindings.append(
            {
                "kind": "agent",
                "id": str(fields.get("id") or key),
                "title": str(fields.get("title") or "").strip(),
                "parent_id": str(fields.get("parentId") or ""),
                "provider": str(fields.get("harnessId") or ""),
                "session_id": str(fields.get("harnessSessionId") or ""),
                "workspace": first_workspace(fields.get("workspaceFolders")),
            }
        )
    return bindings


def read_chats(chats) -> list[dict]:
    bindings = []
    for key in list(chats.keys()):
        rec = chats[key]
        fields = set(rec.keys())
        chain = rec["activeSessionChain"] if "activeSessionChain" in fields else None
        if chain is None:
            continue
        chain_keys = set(chain.keys())
        bindings.append(
            {
                "kind": "chat",
                "id": str(rec["id"]) if "id" in fields else key,
                "title": str(rec["title"] or "").strip() if "title" in fields else "",
                "parent_id": str(rec["parentId"] or "") if "parentId" in fields else "",
                "provider": str(chain["harnessId"] or "") if "harnessId" in chain_keys else "",
                "session_id": str(chain["sessionId"] or "") if "sessionId" in chain_keys else "",
                "workspace": primary_workspace(
                    chain["sessionWorkspaceSnapshot"] if "sessionWorkspaceSnapshot" in chain_keys else None
                ),
            }
        )
    return bindings


def primary_workspace(snapshot) -> str:
    if snapshot is None:
        return ""
    try:
        if "primaryWorkspace" in set(snapshot.keys()):
            return str(snapshot["primaryWorkspace"] or "")
    except AttributeError:
        pass
    return ""


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
