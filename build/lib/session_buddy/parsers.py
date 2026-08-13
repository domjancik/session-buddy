from __future__ import annotations

import json
import os
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
        title = clean_title(str(entry.get("firstPrompt") or ""))
        summary = str(entry.get("summary") or "")
        cwd = str(entry.get("projectPath") or cwd)
        git_branch = str(entry.get("gitBranch") or "")
        created_at = parse_timestamp(entry.get("created"))
        updated_at = parse_timestamp(entry.get("modified"))

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
        summary=truncate(summary, 1000),
        preview=preview,
        messages=messages,
    )


def load_codex_thread_metadata(codex_home: Path) -> dict[str, dict[str, Any]]:
    db_path = codex_home / "state_5.sqlite"
    if not db_path.exists():
        return {}
    query = (
        "select id,title,cwd,created_at_ms,updated_at_ms,git_branch,preview "
        "from threads"
    )
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query).fetchall()
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
        title = str(thread_meta.get("title") or "")
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
