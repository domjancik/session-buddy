from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import AnnotatedTitle, Annotation


@dataclass(slots=True)
class AnnotatorResult:
    annotations: list[Annotation] = field(default_factory=list)
    titles: list[AnnotatedTitle] = field(default_factory=list)
    groups: list[dict[str, str]] = field(default_factory=list)
    """Top-level containers (epics, swarms, projects) for `sb groups`. Free-form per source."""
    warning: str = ""


@runtime_checkable
class Annotator(Protocol):
    source: str
    """Namespace for every key this annotator writes. Must be stable across releases."""

    title_priority: int
    """Tie-break when several annotators claim a title for the same session."""

    def detect(self, home: Path | None = None) -> bool:
        """True when this tool's data is present. Never raise — absence is normal."""

    def collect(self, home: Path | None = None) -> AnnotatorResult:
        """Read the tool's own store. Must not read session transcripts.

        Failures are reported via AnnotatorResult.warning rather than raised: a broken
        annotator degrades search to un-annotated results, it never breaks indexing.
        """
