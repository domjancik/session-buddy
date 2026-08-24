"""Conductor annotator.

Conductor (conductor.build) runs Claude Code and Codex in parallel git worktrees. The
harnesses write the transcripts, so Conductor is an annotator: it contributes the workspace
a session ran in, and the name it gave that session.

Its store is SQLite at `~/Library/Application Support/com.conductor.app/conductor.db`:

    sessions(id, agent_type, claude_session_id, title, workspace_id, updated_at, …)
    workspaces(id, repository_id, directory_name, workspace_name, branch, workspace_path, …)
    repos(id, name, root_path, …)

`sessions.claude_session_id` is our `session_id` and `agent_type` is our `provider` — the
column is named for Claude but holds the id whichever harness ran, and `agent_type` says
which. That pair is the whole join.

The titles matter here: Conductor opens a session by injecting a `<system_instruction>`
block as the first user turn, so the transcript-derived title is that boilerplate for every
session it has ever run, while Conductor itself knows them as "Review repo", "Discuss repo".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..models import AnnotatedTitle, Annotation
from .base import AnnotatorResult

SOURCE = "conductor"
TITLE_PRIORITY = 20  # a per-session name beats an orchestrator's per-agent name
PLACEHOLDER_TITLES = {"", "untitled"}


class ConductorAnnotator:
    source = SOURCE
    title_priority = TITLE_PRIORITY

    def default_home(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "com.conductor.app"

    def db_path(self, home: Path | None = None) -> Path:
        return (home or self.default_home()) / "conductor.db"

    def detect(self, home: Path | None = None) -> bool:
        return self.db_path(home).is_file()

    def collect(self, home: Path | None = None) -> AnnotatorResult:
        result = AnnotatorResult()
        path = self.db_path(home)
        if not path.is_file():
            return result
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as error:
            result.warning = f"conductor: {error}"
            return result

        try:
            rows = conn.execute(
                """
                select s.claude_session_id as session_id,
                       s.agent_type        as provider,
                       s.title             as session_title,
                       s.workspace_id      as workspace_id,
                       w.directory_name    as directory_name,
                       w.workspace_name    as workspace_name,
                       w.branch            as branch,
                       w.workspace_path    as workspace_path,
                       w.updated_at        as workspace_updated_at,
                       r.name              as repo
                from sessions s
                left join workspaces w on w.id = s.workspace_id
                left join repos r on r.id = w.repository_id
                where s.claude_session_id is not null and s.claude_session_id != ''
                """
            ).fetchall()
            workspaces = conn.execute(
                """
                select w.id, w.directory_name, w.workspace_name, w.branch, w.updated_at,
                       r.name as repo
                from workspaces w
                left join repos r on r.id = w.repository_id
                """
            ).fetchall()
        except sqlite3.Error as error:
            result.warning = f"conductor: {error}"
            return result
        finally:
            conn.close()

        for row in rows:
            provider = str(row["provider"] or "")
            session_id = str(row["session_id"] or "")
            if not provider or not session_id:
                continue
            title = clean_title(row["session_title"])
            pairs = {
                "workspace_id": str(row["workspace_id"] or ""),
                "workspace": workspace_label(row),
                "branch": str(row["branch"] or ""),
                "repo": str(row["repo"] or ""),
                "workspace_path": str(row["workspace_path"] or ""),
                "session_title": title,
            }
            for key, value in pairs.items():
                if value:
                    result.annotations.append(Annotation(provider, session_id, SOURCE, key, value))
            if title:
                result.titles.append(
                    AnnotatedTitle(provider, session_id, SOURCE, title, TITLE_PRIORITY)
                )

        for workspace in workspaces:
            label = workspace_label(workspace)
            if not label:
                continue
            result.groups.append(
                {
                    "id": str(workspace["id"] or ""),
                    "title": label,
                    "updated_at": str(to_epoch_ms(workspace["updated_at"])),
                    "key": "workspace_id",
                }
            )
        return result


def clean_title(value: object) -> str:
    title = str(value or "").strip()
    return "" if title.lower() in PLACEHOLDER_TITLES else title


def workspace_label(row: sqlite3.Row) -> str:
    """`repo/worktree` reads better in a listing than either half alone."""
    keys = row.keys()
    name = ""
    for column in ("workspace_name", "directory_name"):
        if column in keys:
            name = str(row[column] or "").strip()
            if name:
                break
    repo = str(row["repo"] or "").strip() if "repo" in keys else ""
    if repo and name:
        return f"{repo}/{name}"
    return name or repo


def to_epoch_ms(value: object) -> int:
    """Conductor writes both `2026-08-24T14:06:29.474Z` and `2026-08-24 14:06:47`."""
    text = str(value or "").strip()
    if not text:
        return 0
    normalised = text.replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)
