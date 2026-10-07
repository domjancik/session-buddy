from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .models import MessageRecord, SessionRecord
from .text import collapse_ws, parse_timestamp, text_from_content, truncate


def load_claude_indexes(claude_home: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    entries: dict[str, dict[str, Any]] = {}
    project_paths: dict[str, str] = {}
    projects_root = claude_home / "projects"
    if not projects_root.exists():
        return entries, project_paths

    for index_path in projects_root.rglob("sessions-index.json"):
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        original_path = data.get("originalPath")
        if isinstance(original_path, str):
            project_paths[str(index_path.parent)] = original_path
        for entry in data.get("entries", []):
            if not isinstance(entry, dict):
                continue
            full_path = entry.get("fullPath")
            if isinstance(full_path, str):
                normalized = Path(full_path)
                if not normalized.is_absolute():
                    normalized = Path.cwd() / normalized
                entries[str(normalized)] = entry
    return entries, project_paths


SUBAGENT_SEPARATOR = "/"
"""Joins a parent session id to its subagent file stem. `/` cannot occur in either."""


def parse_claude_session(path: Path, entry: dict[str, Any] | None, project_path: str | None) -> SessionRecord | None:
    stat = path.stat()
    session_id = path.stem
    title = ""
    summary = ""
    cwd = project_path or ""
    git_branch = ""
    created_at: int | None = None
    updated_at: int | None = None
    messages: list[MessageRecord] = []
    first_prompt = ""

    if entry:
        session_id = str(entry.get("sessionId") or session_id)
        # `/rename` writes customTitle. It is the provider's own title for the session and
        # outranks the derived one: people rename precisely when the first prompt labels the
        # session badly, so the sessions that lose their name are the ones that needed one.
        title = clean_title(str(entry.get("customTitle") or "")) or clean_title(
            str(entry.get("firstPrompt") or "")
        )
        summary = str(entry.get("summary") or "")
        cwd = str(entry.get("projectPath") or cwd)
        git_branch = str(entry.get("gitBranch") or "")
        created_at = parse_timestamp(entry.get("created"))
        updated_at = parse_timestamp(entry.get("modified"))

    # `/rename` appends a `custom-title` record to the transcript. The abandoned
    # sessions-index.json `customTitle` above is kept only for transcripts old enough to
    # still have an index entry; on a current install that file is no longer written.
    custom_titles: list[str] = []

    idx = 0
    for row in stream_jsonl(path):
        row_session_id = row.get("sessionId")
        if isinstance(row_session_id, str):
            session_id = row_session_id
        row_cwd = row.get("cwd")
        if isinstance(row_cwd, str) and row_cwd:
            cwd = row_cwd
        row_branch = row.get("gitBranch")
        if isinstance(row_branch, str) and row_branch:
            git_branch = row_branch

        timestamp = parse_timestamp(row.get("timestamp"))
        if timestamp is not None:
            created_at = timestamp if created_at is None else min(created_at, timestamp)
            updated_at = timestamp if updated_at is None else max(updated_at, timestamp)

        row_type = row.get("type")
        if row_type == "custom-title":
            value = str(row.get("customTitle") or "").strip()
            if value:
                custom_titles.append(value)
            continue
        if row_type not in {"user", "assistant", "summary"}:
            continue
        message = row.get("message")
        role = row_type if row_type in {"user", "assistant"} else "summary"
        if isinstance(message, dict):
            role = str(message.get("role") or role)
            text = text_from_content(message.get("content"))
        else:
            text = text_from_content(row.get("summary") or row.get("content"))
        text = collapse_ws(text)
        if not text:
            continue
        if role == "user" and not first_prompt and not text.startswith("<ide_opened_file>"):
            first_prompt = truncate(text, 500)
        messages.append(MessageRecord("claude", session_id, idx, role, timestamp, text))
        idx += 1

    if not session_id:
        return None

    # A subagent transcript records its PARENT's sessionId, and lives at
    # `<parent>/subagents/agent-*.jsonl`. Keyed on that id it is not a second session, it is
    # the same one: upsert deletes and reinserts, so every subagent overwrote the parent and
    # each other, and whichever file was scanned last was all that survived. Measured on one
    # machine: 91 subagent transcripts, up to 24 collapsing onto a single id, several over a
    # megabyte, none of them searchable. Give each its own id so they coexist.
    if path.parent.name == "subagents":
        session_id = f"{session_id}{SUBAGENT_SEPARATOR}{path.stem}"

    # A launcher can set the title too (Traycer starts Claude with `--name`), and it writes
    # the same record a person's /rename does - measured, they are byte-identical bar the
    # value. What separates them is that a launcher's name is written once at startup and
    # never changes, so a value that CHANGES within one transcript is a person renaming.
    # A constant one is taken as the session's title but left outranked by annotators, which
    # is what restores a real title to the 156 sessions a launcher had named the same thing.
    renamed_by_user = len(set(custom_titles)) > 1
    if custom_titles:
        title = custom_titles[-1]

    if not cwd:
        cwd = infer_cwd_from_claude_project(path.parent.name)
    if not title:
        title = clean_title(first_prompt) or clean_title(summary) or session_id
    if not first_prompt:
        first_prompt = title
    if updated_at is None:
        updated_at = int(stat.st_mtime * 1000)
    if created_at is None:
        created_at = updated_at
    preview = build_preview(messages, summary)

    return SessionRecord(
        provider="claude",
        session_id=session_id,
        title=truncate(title, 240),
        cwd=cwd,
        created_at=created_at,
        updated_at=updated_at,
        git_branch=git_branch,
        source_path=str(path),
        file_mtime=int(stat.st_mtime_ns),
        file_size=stat.st_size,
        message_count=len(messages),
        first_prompt=truncate(first_prompt, 1000),
        renamed_by_user=renamed_by_user,
        titled=bool(custom_titles),
        summary=truncate(summary, 1000),
        preview=preview,
        messages=messages,
    )


def prefer_rename(name: str, derived: str) -> str:
    """Pick the user's name over the derived title, unless it is a truncation of it.

    Not every `threads.name` is a rename someone typed. Measured across 1300 threads, 8 had a
    name and 3 of those were the prompt cut at ~35 characters, mid-sentence - Codex's rename
    box appears to pre-fill from the prompt, and accepting that unchanged stores a name that
    is strictly less informative than the title it would replace. So a name that is merely a
    prefix of the derived title loses to it; anything else wins.

    Claude's `customTitle` gets no such guard: nothing in its store writes one automatically,
    and a guard there would only risk discarding a real rename that happens to read as a prefix.
    """
    name = name.strip()
    if not name:
        return derived.strip()
    squashed_name = " ".join(name.split()).casefold()
    squashed_derived = " ".join(derived.split()).casefold()
    if squashed_derived.startswith(squashed_name) and len(squashed_derived) > len(squashed_name):
        return derived.strip()
    return name


def load_codex_thread_metadata(codex_home: Path) -> dict[str, dict[str, Any]]:
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        return {}
    wanted = ["id", "title", "cwd", "created_at_ms", "updated_at_ms", "git_branch", "preview", "name"]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        # `name` holds a /rename and arrived in a later Codex release, so select what this
        # database actually has rather than failing the whole read on an older schema.
        present = {row["name"] for row in conn.execute("pragma table_info(threads)")}
        columns = [column for column in wanted if column in present]
        if "id" not in columns:
            return {}
        rows = conn.execute(f"select {','.join(columns)} from threads").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
    return {str(row["id"]): dict(row) for row in rows}


def load_codex_session_index(codex_home: Path) -> dict[str, dict[str, Any]]:
    index_path = codex_home / "session_index.jsonl"
    result: dict[str, dict[str, Any]] = {}
    if not index_path.exists():
        return result
    for row in stream_jsonl(index_path):
        session_id = row.get("id")
        if isinstance(session_id, str):
            result[session_id] = row
    return result


def parse_codex_session(
    path: Path,
    thread_meta: dict[str, Any] | None,
    index_meta: dict[str, Any] | None,
) -> SessionRecord | None:
    stat = path.stat()
    session_id = ""
    title = ""
    cwd = ""
    git_branch = ""
    created_at: int | None = None
    updated_at: int | None = None
    preview = ""
    messages: list[MessageRecord] = []
    first_prompt = ""

    if thread_meta:
        session_id = str(thread_meta.get("id") or "")
        # `name` is a /rename; `title` is Codex's own summary of the first prompt.
        title = prefer_rename(
            str(thread_meta.get("name") or ""), str(thread_meta.get("title") or "")
        )
        cwd = str(thread_meta.get("cwd") or "")
        git_branch = str(thread_meta.get("git_branch") or "")
        created_at = parse_timestamp(thread_meta.get("created_at_ms"))
        updated_at = parse_timestamp(thread_meta.get("updated_at_ms"))
        preview = str(thread_meta.get("preview") or "")
    if index_meta:
        session_id = session_id or str(index_meta.get("id") or "")
        title = title or str(index_meta.get("thread_name") or "")
        updated_at = updated_at or parse_timestamp(index_meta.get("updated_at"))

    idx = 0
    for row in stream_jsonl(path):
        row_type = row.get("type")
        timestamp = parse_timestamp(row.get("timestamp"))
        if timestamp is not None:
            created_at = timestamp if created_at is None else min(created_at, timestamp)
            updated_at = timestamp if updated_at is None else max(updated_at, timestamp)

        if row_type == "session_meta":
            payload = row.get("payload")
            if isinstance(payload, dict):
                session_id = str(payload.get("id") or session_id)
                cwd = str(payload.get("cwd") or cwd)
                git = payload.get("git")
                if isinstance(git, dict):
                    git_branch = str(git.get("branch") or git_branch)
            continue

        payload = row.get("payload")
        role = ""
        text = ""
        if row_type == "response_item" and isinstance(payload, dict):
            payload_type = payload.get("type")
            role = str(payload.get("role") or payload_type or "")
            if payload_type in {"message", "function_call", "function_call_output"}:
                text = text_from_content(payload.get("content") or payload)
        elif row_type in {"message", "user", "assistant"}:
            role = str(row.get("role") or row_type)
            text = text_from_content(row.get("content") or row.get("message"))

        text = collapse_ws(text)
        if not text:
            continue
        if role == "user" and not first_prompt:
            first_prompt = truncate(text, 500)
        messages.append(MessageRecord("codex", session_id, idx, role or "item", timestamp, text))
        idx += 1

    if not session_id:
        session_id = infer_codex_id_from_filename(path.name)
    if not session_id:
        return None
    if not title:
        title = clean_title(first_prompt) or session_id
    if not first_prompt:
        first_prompt = title
    if not preview:
        preview = build_preview(messages, "")
    if updated_at is None:
        updated_at = int(stat.st_mtime * 1000)
    if created_at is None:
        created_at = updated_at

    return SessionRecord(
        provider="codex",
        session_id=session_id,
        title=truncate(title, 240),
        cwd=cwd,
        created_at=created_at,
        updated_at=updated_at,
        git_branch=git_branch,
        source_path=str(path),
        file_mtime=int(stat.st_mtime_ns),
        file_size=stat.st_size,
        message_count=len(messages),
        first_prompt=truncate(first_prompt, 1000),
        summary="",
        preview=truncate(preview, 4000),
        messages=messages,
    )


def stream_jsonl(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def build_preview(messages: list[MessageRecord], summary: str) -> str:
    parts: list[str] = []
    if summary:
        parts.append(summary)
    for message in messages:
        if message.role in {"user", "assistant"}:
            parts.append(f"{message.role}: {message.text}")
        if len(" ".join(parts)) > 3500:
            break
    return truncate("\n".join(parts), 4000)


def clean_title(value: str) -> str:
    value = collapse_ws(value)
    if not value or value.lower() == "no prompt":
        return ""
    return value


def infer_cwd_from_claude_project(dirname: str) -> str:
    if not dirname.startswith("-"):
        return dirname
    return "/" + dirname[1:].replace("-", "/")


def infer_codex_id_from_filename(filename: str) -> str:
    stem = filename.removesuffix(".jsonl")
    parts = stem.split("-")
    if len(parts) >= 6:
        return "-".join(parts[-5:])
    return stem


def scan_claude_sessions(claude_home: Path) -> list[Path]:
    root = claude_home / "projects"
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def scan_codex_sessions(codex_home: Path) -> list[Path]:
    root = codex_home / "sessions"
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def safe_stat_key(path: Path) -> tuple[int, int]:
    stat = os.stat(path)
    return int(stat.st_mtime_ns), int(stat.st_size)


OPENCODE_DB_NAME = "opencode.db"


def default_opencode_home() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return root / "opencode"


def opencode_db_path(opencode_home: Path) -> Path:
    return opencode_home / OPENCODE_DB_NAME


def opencode_source_path(db_path: Path, session_id: str) -> str:
    """Identity for a session inside a shared database.

    Claude and Codex give each session its own file, so source_path is that file. opencode
    keeps every session in one SQLite database, so we synthesise a per-session key. It is
    never opened as a file: freshness stats the database and compares a per-session
    fingerprint, otherwise one write would mark every opencode session stale.
    """
    return f"{db_path}#{session_id}"


def opencode_stat_key(row: dict[str, Any]) -> tuple[int, int]:
    """Per-session change key, standing in for (mtime, size) of a private file.

    Every opencode session shares one database, so its mtime moves whenever *any* session
    is written. Keying on the session's own time_updated keeps unrelated sessions skippable.
    """
    return to_int(row.get("time_updated")) or 0, to_int(row.get("time_created")) or 0


def opencode_metadata_fingerprint(row: dict[str, Any]) -> str:
    return json.dumps(
        {
            "time_updated": row.get("time_updated"),
            "title": row.get("title"),
            "directory": row.get("directory"),
            "archived": row.get("time_archived"),
        },
        sort_keys=True,
    )


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def scan_opencode_sessions(opencode_home: Path) -> list[dict[str, Any]]:
    db_path = opencode_db_path(opencode_home)
    if not db_path.is_file():
        return []
    try:
        conn = connect_readonly(db_path)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            """
            select id, parent_id, slug, directory, title, time_created, time_updated,
                   time_archived, agent, model
            from session
            order by time_updated desc
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [dict(row) for row in rows]


def parse_opencode_session(db_path: Path, row: dict[str, Any]) -> SessionRecord | None:
    session_id = str(row.get("id") or "")
    if not session_id:
        return None
    try:
        conn = connect_readonly(db_path)
    except sqlite3.Error:
        return None
    try:
        message_rows = conn.execute(
            "select id, data, time_created from message where session_id = ? order by time_created, id",
            (session_id,),
        ).fetchall()
        part_rows = conn.execute(
            "select message_id, data, time_created from part where session_id = ? order by time_created, id",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    parts_by_message: dict[str, list[str]] = {}
    for part in part_rows:
        text = opencode_part_text(part["data"])
        if text:
            parts_by_message.setdefault(str(part["message_id"]), []).append(text)

    messages: list[MessageRecord] = []
    for message in message_rows:
        text = "\n".join(parts_by_message.get(str(message["id"]), []))
        if not text.strip():
            continue
        messages.append(
            MessageRecord(
                provider="opencode",
                session_id=session_id,
                idx=len(messages),
                role=opencode_role(message["data"]),
                timestamp=to_int(message["time_created"]),
                text=collapse_ws(text),
            )
        )

    first_prompt = next((m.text for m in messages if m.role == "user"), "")
    title = clean_title(str(row.get("title") or "")) or truncate(first_prompt, 120)
    mtime, size = opencode_stat_key(row)
    return SessionRecord(
        provider="opencode",
        session_id=session_id,
        title=title,
        cwd=str(row.get("directory") or ""),
        created_at=to_int(row.get("time_created")),
        updated_at=to_int(row.get("time_updated")),
        git_branch="",
        source_path=opencode_source_path(db_path, session_id),
        file_mtime=mtime,
        file_size=size,
        message_count=len(messages),
        first_prompt=truncate(first_prompt, 400),
        summary="",
        preview=build_preview(messages, ""),
        messages=messages,
    )


def opencode_part_text(data: Any) -> str:
    """Parts hold tool calls, reasoning and text; only text carries searchable content."""
    payload = load_json(data)
    if not isinstance(payload, dict):
        return ""
    if payload.get("type") != "text":
        return ""
    return str(payload.get("text") or "")


def opencode_role(data: Any) -> str:
    payload = load_json(data)
    if isinstance(payload, dict):
        return str(payload.get("role") or "unknown")
    return "unknown"


def load_json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


PI_SESSION_GLOBS = ("agent/sessions/*/*.jsonl", "profiles/*/sessions/*/*.jsonl")


def default_pi_home() -> Path:
    return Path.home() / ".pi"


def scan_pi_sessions(pi_home: Path) -> list[Path]:
    """pi writes one JSONL per session, under the default agent and under each profile."""
    if not pi_home.is_dir():
        return []
    paths: list[Path] = []
    for pattern in PI_SESSION_GLOBS:
        paths.extend(sorted(pi_home.glob(pattern)))
    return paths


def parse_pi_session(path: Path) -> SessionRecord | None:
    """Parse a pi transcript.

    Records are one JSON object per line, tagged by `type`:
      session                 - header: id, version, timestamp, cwd
      message                 - {id, parentId, timestamp, message: {role, content, timestamp}}
      model_change /
      thinking_level_change   - settings events, no conversation content

    `message.role` is user, assistant, or toolResult, and `content` is a block list of
    `text`, `thinking`, and `toolCall`. Only `text` carries searchable prose: thinking holds
    reasoning plus an encrypted signature blob, and toolCall holds arguments.
    """
    header: dict[str, Any] = {}
    messages: list[MessageRecord] = []
    session_id = ""

    for record in stream_jsonl(path):
        kind = record.get("type")
        if kind == "session":
            header = record
            session_id = str(record.get("id") or "")
            continue
        if kind != "message":
            continue
        payload = record.get("message") or {}
        text = pi_message_text(payload.get("content"))
        if not text.strip():
            continue
        messages.append(
            MessageRecord(
                provider="pi",
                session_id=session_id,
                idx=len(messages),
                role=str(payload.get("role") or "unknown"),
                timestamp=parse_timestamp(record.get("timestamp")),
                text=collapse_ws(text),
            )
        )

    if not messages:
        # Header-only or unreadable: nothing to search, so keep it out of the index
        # rather than adding an empty row that can never match.
        return None
    if not session_id:
        session_id = infer_pi_id_from_filename(path.name)
    if not session_id:
        return None
    for message in messages:
        message.session_id = session_id

    cwd = str(header.get("cwd") or "") or infer_cwd_from_pi_dir(path.parent.name)
    first_prompt = next((m.text for m in messages if m.role == "user"), "")
    created_at = parse_timestamp(header.get("timestamp"))
    updated_at = messages[-1].timestamp if messages else created_at
    mtime, size = safe_stat_key(path)
    return SessionRecord(
        provider="pi",
        session_id=session_id,
        title=truncate(first_prompt, 120),
        cwd=cwd,
        created_at=created_at,
        updated_at=updated_at or created_at,
        git_branch="",
        source_path=str(path),
        file_mtime=mtime,
        file_size=size,
        message_count=len(messages),
        first_prompt=truncate(first_prompt, 400),
        summary="",
        preview=build_preview(messages, ""),
        messages=messages,
    )


PI_SKILL_INJECTION = re.compile(r"<skill\b[^>]*>.*?</skill>", re.S)


def pi_message_text(content: Any) -> str:
    if isinstance(content, str):
        return strip_pi_injections(content)
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return strip_pi_injections("\n".join(parts))


def strip_pi_injections(text: str) -> str:
    """Drop skill documents pi prepends to a user turn.

    pi injects the whole SKILL.md into the user message, then appends what the user actually
    typed. Left in, it becomes the session title and the preview, and every session that
    loaded a skill looks alike. The document is not conversation, so it is dropped from the
    indexed text rather than merely hidden from the title.
    """
    return PI_SKILL_INJECTION.sub("", text).strip()


def infer_pi_id_from_filename(filename: str) -> str:
    """`2026-08-23T13-11-05-050Z_01a02ebf-...jsonl` -> the uuid after the underscore."""
    stem = filename[:-6] if filename.endswith(".jsonl") else filename
    _, _, tail = stem.partition("_")
    return tail or stem


def infer_cwd_from_pi_dir(dirname: str) -> str:
    """`--Users-dominikj-dev-oss-session-buddy--` -> a best-effort path.

    Lossy: pi encodes both separators and literal dashes as `-`, so a directory whose name
    contains a dash is indistinguishable from a path separator. Only used when the session
    header has no cwd, which should not happen for v3 transcripts.
    """
    trimmed = dirname.strip("-")
    return "/" + trimmed.replace("-", "/") if trimmed else ""
