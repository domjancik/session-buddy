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

Traycer keeps a *second* store, and it is not a mirror of the first:
`~/.traycer/host/epic-state/<epic_id>/chat/chat.db`, an event-sourced SQLite log whose
`chat_projection.projection_json` holds the materialised chat, with the same binding under
`tuiAgent.{harnessId, harnessSessionId}`.

The two stores disagree about the same epic. Measured on one machine: 197 bound sessions in
the Yjs seeds, 82 in the chat stores, and *zero* in both. One epic's seed reported
`tuiAgents: 0, chats: 16` while its chat.db held all 46 chats, every binding among them. So a
seed that decodes cleanly is not evidence its epic is covered, and an epic missing from one
store says nothing about the other. Read both and merge.

Only the chat store carries a binding when Traycer wrote it there, so a session absent from
both is genuinely unorchestrated - but absence from the seeds alone means nothing.

`desktop-windows.json` also holds epic names, but only for tabs that happen to be open,
so it is not a usable source.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..models import AnnotatedTitle, Annotation
from .base import AnnotatorResult

SOURCE = "traycer"
TITLE_PRIORITY = 10

# Only the epic record carries an epic title; the chat store knows its epic by directory name.
CHAT_STORE = "host/epic-state"
# A third store. Epic titles have migrated out of the Yjs seeds into a host-level database,
# so a seed that decodes with no `title` key is not an untitled epic - its name lives here.
# Measured: 26 of 118 epics had no title from the seeds, and 11 of those are named in this
# one, with zero disagreements where both have a name.
EPIC_HOMES = "host/epic-homes/epic-homes.db"


class TraycerAnnotator:
    source = SOURCE
    title_priority = TITLE_PRIORITY

    def default_home(self) -> Path:
        return Path.home() / ".traycer"

    def detect(self, home: Path | None = None) -> bool:
        root = home or self.default_home()
        # Either store alone is enough: they cover different epics, and a machine can have
        # chat stores without any decodable seed.
        return (root / "epics").is_dir() or (root / CHAT_STORE).is_dir()

    def collect(self, home: Path | None = None) -> AnnotatorResult:
        root = home or self.default_home()
        result = AnnotatorResult()
        warnings: list[str] = []

        epic_titles: dict[str, str] = read_epic_homes(root / EPIC_HOMES)
        grouped: set[str] = set()

        warnings += self.collect_seeds(root, result, epic_titles, grouped)
        warnings += self.collect_chat_stores(root, result, epic_titles, grouped)
        result.warning = "; ".join(warnings)
        return result

    def collect_seeds(
        self, root: Path, result: AnnotatorResult, epic_titles: dict[str, str], grouped: set[str]
    ) -> list[str]:
        """Yjs epic seeds. Returns any warnings; never raises."""
        epics_dir = root / "epics"
        if not epics_dir.is_dir():
            return []
        try:
            from pycrdt import Doc, Map
        except ImportError:
            # The extra is optional: without it the seeds are unreadable, but the chat
            # store is plain SQLite and still contributes.
            return [
                "traycer seeds skipped: install the extra with "
                "`uv tool install 'session-buddy[traycer]'`"
            ]

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
            if record["title"]:
                epic_titles[record["id"]] = record["title"]
            elif epic_titles.get(record["id"]):
                record["title"] = epic_titles[record["id"]]
            self.emit_epic(result, record, grouped)
        return [f"traycer: {failed} epic seed(s) could not be decoded"] if failed else []

    def collect_chat_stores(
        self, root: Path, result: AnnotatorResult, epic_titles: dict[str, str], grouped: set[str]
    ) -> list[str]:
        """Per-epic chat.db stores. Returns any warnings; never raises.

        Runs after the seeds so that where both stores name the same session, the chat store
        wins: writes are `insert or replace`, so the later annotation is the one that lands,
        and the chat store is the one Traycer keeps current.
        """
        state_dir = root / CHAT_STORE
        if not state_dir.is_dir():
            return []
        failed = 0
        for epic_dir in sorted(state_dir.iterdir()):
            db_path = epic_dir / "chat" / "chat.db"
            if not db_path.is_file():
                continue
            try:
                record = read_chat_store(db_path, epic_dir.name, epic_titles.get(epic_dir.name, ""))
            except (sqlite3.Error, OSError, ValueError):
                failed += 1
                continue
            if record["bindings"]:
                self.emit_epic(result, record, grouped)
        return [f"traycer: {failed} chat store(s) could not be read"] if failed else []

    def emit_epic(self, result: AnnotatorResult, record: dict, grouped: set[str]) -> None:
        """Turn one epic's bindings into annotations, titles, and at most one group."""
        if record["id"] not in grouped:
            grouped.add(record["id"])
            result.groups.append(
                {
                    "id": record["id"],
                    "title": record["title"],
                    "updated_at": str(record["updated_at"]),
                    "sessions": str(len(record["bindings"])),
                    "key": "epic_id",  # the annotation key whose value is this group's id
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
                # The id alone cannot be acted on: a chat under a coordinator is opened by
                # the PARENT's name, and resolving it meant walking the store by hand.
                "parent_title": record.get("titles_by_id", {}).get(binding["parent_id"], ""),
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
                "titles_by_id": {b["id"]: b["title"] for b in bindings if b["id"] and b["title"]},
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


# Traycer has shipped two shapes of this table. `chat_projection` stored the whole chat as
# one `projection_json` blob; `chat_projection_head` normalises it into columns and keeps
# only the nested objects as json. A release upgrades the store in place, so both are read:
# selecting the wrong one fails the whole store, which is how 72 of 72 went silently dark.
CHAT_TABLES = ("chat_projection_head", "chat_projection")


def read_epic_homes(db_path: Path) -> dict[str, str]:
    """Epic id to title, from the host-level epic database.

    `local_epic.list_projection_json` is the record the epic list renders from; its `title`
    is what someone searches Traycer by. Failures are silent: this is a supplementary source
    and an epic with no name here simply keeps whatever its seed gave it.
    """
    if not db_path.is_file():
        return {}
    titles: dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute("select epic_id, list_projection_json from local_epic").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    for epic_id, payload in rows:
        try:
            projection = json.loads(payload or "{}")
        except json.JSONDecodeError:
            continue
        title = str(projection.get("title") or "").strip() if isinstance(projection, dict) else ""
        if epic_id and title:
            titles[str(epic_id)] = title
    return titles


def read_chat_rows(conn: sqlite3.Connection) -> list[dict]:
    """Every chat in this store, normalised to the nested shape the rest of the code uses."""
    present = {
        row[0]
        for row in conn.execute("select name from sqlite_master where type = 'table'")
    }
    for table in CHAT_TABLES:
        if table not in present:
            continue
        if table == "chat_projection":
            rows = []
            for (payload,) in conn.execute(f"select projection_json from {table}"):
                try:
                    rows.append(json.loads(payload or "{}"))
                except json.JSONDecodeError:
                    continue  # one malformed projection must not cost us the epic
            return rows
        conn.row_factory = sqlite3.Row
        return [head_row_to_chat(row) for row in conn.execute(f"select * from {table}")]
    return []


def head_row_to_chat(row: sqlite3.Row) -> dict:
    """Rebuild the nested chat record from the columnar `chat_projection_head` row."""

    def nested(column: str) -> dict:
        try:
            value = json.loads(row[column] or "{}")
        except (json.JSONDecodeError, IndexError, KeyError):
            return {}
        return value if isinstance(value, dict) else {}

    columns = row.keys()
    return {
        "chatId": row["chat_id"] if "chat_id" in columns else "",
        "tenantKind": row["tenant_kind"] if "tenant_kind" in columns else "",
        "parentChatId": row["parent_chat_id"] if "parent_chat_id" in columns else "",
        "title": row["title"] if "title" in columns else "",
        "updatedAt": row["updated_at"] if "updated_at" in columns else 0,
        "tuiAgent": nested("tui_agent_json") or None,
        "hostPrivate": nested("host_private_json"),
        # The head table keeps messages in their own rows; the only one this reader wanted
        # was each message's sessionAnchor, so a resumed GUI chat now contributes just its
        # live session rather than its earlier ones.
        "messages": [],
    }


def read_chat_store(db_path: Path, epic_id: str, epic_title: str) -> dict:
    """Read one epic's chat.db into the same record shape the Yjs seeds produce.

    Opened read-only against the live file. Traycer runs in WAL mode, and a `mode=ro`
    connection follows the WAL, so a running Traycer neither blocks the read nor hides its
    newest chats - verified by counting every store both ways and getting the same 97.
    Copying the db without its `-wal` sidecar is what silently under-reports.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = read_chat_rows(conn)
    finally:
        conn.close()

    bindings: list[dict] = []
    titles_by_chat: dict[str, str] = {}
    updated_at = 0
    for chat in rows:
        if not isinstance(chat, dict):
            continue
        updated_at = max(updated_at, int(chat.get("updatedAt") or 0))
        chat_id = str(chat.get("chatId") or "")
        chat_title = str(chat.get("title") or "").strip()
        if chat_id and chat_title:
            # Keyed by chat id as well as binding id: a parent is referenced by its CHAT id
            # even when its own binding is keyed by the agent id inside it.
            titles_by_chat[chat_id] = chat_title

        agent = chat.get("tuiAgent")
        if isinstance(agent, dict):
            bindings.append(
                {
                    # A terminal agent (`tenantKind: tui-agent`), so it shares the `agent_*`
                    # keys with the seeds' tuiAgents rather than `chat_*`.
                    "kind": "agent",
                    "id": str(agent.get("id") or chat.get("chatId") or ""),
                    # The chat and its agent carry the same title in every record measured;
                    # the chat's is preferred because a renamed tab writes there.
                    "title": str(chat.get("title") or agent.get("title") or "").strip(),
                    "parent_id": str(agent.get("parentId") or chat.get("parentChatId") or ""),
                    "provider": str(agent.get("harnessId") or ""),
                    "session_id": str(agent.get("harnessSessionId") or ""),
                    "workspace": first_workspace(agent.get("workspaceFolders")),
                }
            )
            continue
        bindings.extend(read_gui_chat(chat))
    titles_by_id = {b["id"]: b["title"] for b in bindings if b["id"] and b["title"]}
    titles_by_id.update(titles_by_chat)
    return {
        "id": epic_id,
        "title": epic_title,
        "updated_at": updated_at,
        "bindings": bindings,
        "titles_by_id": titles_by_id,
    }


def read_gui_chat(chat: dict) -> list[dict]:
    """Bindings for a desktop-app chat (`tenantKind: chat`), which has no `tuiAgent`.

    Its binding is nested at `hostPrivate.data.activeSessionChain`, *not* at the top level
    where the Yjs `chats[]` entries keep theirs. Probing only the top level finds nothing and
    makes a store of GUI chats look like a store of terminal agents - which is the same trap
    as reading only `tuiAgents` from the seeds, one store further in.

    `activeSessionChain` names the live session; a resumed or forked chat leaves its earlier
    sessions behind in each message's `sessionAnchor`. Those are the same chat on the same
    harness, so they are bound too rather than left unsearchable.
    """
    private = chat.get("hostPrivate")
    data = private.get("data") if isinstance(private, dict) else None
    chain = data.get("activeSessionChain") if isinstance(data, dict) else None
    if not isinstance(chain, dict):
        return []

    title = str(chat.get("title") or "").strip()
    chat_id = str(chat.get("chatId") or "")
    parent_id = str(chat.get("parentChatId") or "")

    def binding(source: dict) -> dict:
        return {
            "kind": "chat",
            "id": chat_id,
            "title": title,
            "parent_id": parent_id,
            "provider": str(source.get("harnessId") or ""),
            "session_id": str(source.get("sessionId") or ""),
            "workspace": primary_workspace(source.get("sessionWorkspaceSnapshot")),
        }

    bindings = [binding(chain)]
    seen = {str(chain.get("sessionId") or "")}
    for message in chat.get("messages") or []:
        body = message.get("body") if isinstance(message, dict) else None
        anchor = body.get("sessionAnchor") if isinstance(body, dict) else None
        if not isinstance(anchor, dict):
            continue
        session_id = str(anchor.get("sessionId") or "")
        if session_id and session_id not in seen:
            seen.add(session_id)
            bindings.append(binding(anchor))
    return bindings
