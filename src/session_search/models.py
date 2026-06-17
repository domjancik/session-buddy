from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class MessageRecord:
    provider: str
    session_id: str
    idx: int
    role: str
    timestamp: int | None
    text: str


@dataclass(slots=True)
class SessionRecord:
    provider: str
    session_id: str
    title: str
    cwd: str
    created_at: int | None
    updated_at: int | None
    git_branch: str
    source_path: str
    file_mtime: int
    file_size: int
    message_count: int
    first_prompt: str
    summary: str
    preview: str
    metadata_fingerprint: str = ""
    messages: list[MessageRecord] = field(default_factory=list)


@dataclass(slots=True)
class SearchResult:
    provider: str
    session_id: str
    title: str
    cwd: str
    updated_at: int | None
    git_branch: str
    score: float
    fts_score: float
    fuzzy_score: float
    semantic_score: float
    recency_score: float
    snippets: list[str]


@dataclass(slots=True)
class SourceState:
    source_path: str
    file_mtime: int
    file_size: int
    metadata_fingerprint: str
    session_id: str
    provider: str
