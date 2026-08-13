"""Annotators: tools that describe sessions they did not author.

A *provider* owns a transcript and produces session rows. An *annotator* attaches
namespaced facts to sessions that already exist, keyed by (provider, session_id).

Orchestrators such as Traycer and Omnigent group and label sessions that run under
Claude Code or Codex, so they belong here. A tool may be both: if Omnigent gains
native agents with their own transcripts, it gets a parser for those *and* keeps this
annotator for the orchestration metadata around everybody else's.

Only `title` is ever promoted into the core schema. Everything else stays namespaced
under the annotator's `source`, because "epic", "swarm", and "project" are different
ideas that would lose meaning if flattened into one column.
"""

from __future__ import annotations

from .base import Annotator, AnnotatorResult
from .traycer import TraycerAnnotator

ANNOTATORS: list[Annotator] = [TraycerAnnotator()]

__all__ = ["ANNOTATORS", "Annotator", "AnnotatorResult", "TraycerAnnotator"]
