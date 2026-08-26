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


@dataclass(slots=True)
class Annotation:
    """A namespaced fact contributed by a tool that did not author the transcript.

    Orchestrators (Traycer, Omnigent) group and label sessions that a provider
    already owns. They are keyed to an existing session and never create one.
    """

    provider: str
    session_id: str
    source: str
    key: str
    value: str


@dataclass(slots=True)
class AnnotatedTitle:
    """A title an annotator claims for a session, with the priority to resolve ties."""

    provider: str
    session_id: str
    source: str
    title: str
    priority: int = 0


@dataclass(slots=True)
class BranchRef:
    """A branch a session touched, and how that was observed.

    `evidence` is one of start, push, worktree, status, checkout — kept so a surprising
    match can be explained rather than merely trusted.
    """

    provider: str
    session_id: str
    branch: str
    evidence: str
