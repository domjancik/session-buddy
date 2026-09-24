"""The per-session detail lines, rendered once for every surface.

The CLI grew this formatting inline, so the TUI simply never got it: branches, annotator
metadata and the title's provenance were CLI-only for five releases while the TUI showed a
session's *start* branch and called it "branch". Both surfaces now render from here, and a
test asserts they produce the same lines - a new fact shows up in both or neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checkers
    from .database import IndexDatabase


def detail_lines(db: IndexDatabase, provider: str, session_id: str) -> list[str]:
    """Branches this session touched, then each annotator's namespaced facts."""
    lines: list[str] = []
    refs = db.branches_for(provider, session_id)
    if refs:
        lines.append("branches: " + ", ".join(f"{ref.branch} ({ref.evidence})" for ref in refs))
    for source, pairs in db.annotations_for(provider, session_id).items():
        lines.append(f"{source}: " + " ".join(f"{key}={value}" for key, value in pairs.items()))
    return lines


def title_origin(title_source: str) -> str:
    """A short marker for where a title came from, or "" when it is the first prompt.

    Worth showing because the three cases are read differently: a name someone typed is a
    deliberate label, an annotator's is the orchestrator's, and a provider title is usually
    just the opening message.
    """
    if title_source == "user":
        return "renamed"
    if title_source and title_source != "provider":
        return title_source
    return ""
