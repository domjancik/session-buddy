from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any


WORD_RE = re.compile(r"[A-Za-z0-9_./:-]+")


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, limit: int) -> str:
    text = collapse_ws(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def parse_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            return int(number)
        return int(number * 1000)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def format_time(ms: int | None) -> str:
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def text_from_content(content: Any) -> str:
    parts: list[str] = []
    collect_content(content, parts)
    return collapse_ws("\n".join(part for part in parts if part))


def collect_content(value: Any, parts: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        parts.append(value)
        return
    if isinstance(value, (int, float, bool)):
        parts.append(str(value))
        return
    if isinstance(value, list):
        for item in value:
            collect_content(item, parts)
        return
    if not isinstance(value, dict):
        return

    item_type = value.get("type")
    if isinstance(value.get("text"), str):
        parts.append(value["text"])
    if isinstance(value.get("input_text"), str):
        parts.append(value["input_text"])
    if isinstance(value.get("output_text"), str):
        parts.append(value["output_text"])
    if isinstance(value.get("content"), (str, list, dict)):
        collect_content(value["content"], parts)

    if item_type in {"tool_use", "function_call"}:
        name = value.get("name") or value.get("call_id") or "tool"
        tool_input = value.get("input") or value.get("arguments")
        if isinstance(tool_input, str):
            rendered = tool_input
        else:
            rendered = json.dumps(tool_input, sort_keys=True) if tool_input else ""
        parts.append(collapse_ws(f"{name} {rendered}"))


def fts_query(query: str) -> str:
    terms = [term.strip('"') for term in WORD_RE.findall(query.lower())]
    terms = [term for term in terms if term]
    if not terms:
        return ""
    escaped = [term.replace('"', '""') for term in terms[:12]]
    return " AND ".join(f'"{term}"*' for term in escaped)


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in WORD_RE.finditer(text)]


def read_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows
