from __future__ import annotations

import difflib
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .database import IndexDatabase
from .embeddings import HASH_BACKEND, Embedder, cosine, unpack_vector
from .models import SearchResult
from .text import collapse_ws, format_time, fts_query, truncate


@dataclass(slots=True)
class Candidate:
    provider: str
    session_id: str
    fts_score: float = 0.0
    fuzzy_score: float = 0.0
    title_score: float = 0.0
    semantic_score: float = 0.0
    recency_score: float = 0.0
    snippets: list[str] = field(default_factory=list)


def title_match(query: str, title: str) -> float:
    """How strongly a query names this session, rather than merely occurring inside it.

    Someone who titles a session is telling us what to find it by, so that title has to
    outrank a transcript that happens to repeat the words. Without this, a named session
    loses to any long conversation mentioning the same phrase - measured: searching a
    session's own name returned it 4th, behind three transcripts that merely discussed it.
    """
    q = " ".join(query.split()).casefold()
    t = " ".join(title.split()).casefold()
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    if q in t:
        return 0.6
    return 0.0


def search_sessions(
    db_path: Path,
    query: str,
    limit: int = 20,
    provider: str | None = None,
    cwd: str | None = None,
    semantic: bool = True,
    ext: list[str] | None = None,
    branch: str | None = None,
) -> list[SearchResult]:
    db = IndexDatabase(db_path)
    try:
        return SearchEngine(db.conn).search(query, limit, provider, cwd, semantic, ext, branch)
    finally:
        db.close()


class SearchEngine:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def search(
        self,
        query: str,
        limit: int = 20,
        provider: str | None = None,
        cwd: str | None = None,
        semantic: bool = True,
        ext: list[str] | None = None,
        branch: str | None = None,
    ) -> list[SearchResult]:
        query = collapse_ws(query)
        if not query:
            return []
        candidates: dict[tuple[str, str], Candidate] = {}

        self.add_fts_candidates(candidates, query, provider, cwd, ext, branch)
        self.add_fuzzy_candidates(candidates, query, provider, cwd, ext, branch)
        if semantic:
            self.add_semantic_candidates(candidates, query, provider, cwd, ext, branch)

        if not candidates:
            return []

        sessions = self.load_sessions(list(candidates))
        if not sessions:
            return []

        max_updated = max((row["updated_at"] or 0 for row in sessions.values()), default=0)
        results: list[SearchResult] = []
        for key, candidate in candidates.items():
            row = sessions.get(key)
            if row is None:
                continue
            candidate.recency_score = recency_score(row["updated_at"], max_updated)
            # Only a chosen name earns this; a title derived from the first prompt
            # would otherwise be scored twice, once here and once as its own text.
            if row["titled"]:
                candidate.title_score = title_match(query, str(row["title"] or ""))
            score = (
                candidate.fts_score * 0.45
                + candidate.semantic_score * 0.35
                + candidate.fuzzy_score * 0.15
                + candidate.recency_score * 0.05
                # Added on top rather than folded into the weights above, so every other
                # ranking stays exactly as it was and only a title hit moves. 0.5 is chosen
                # against the ceiling of the content signal, not tuned to a case: the best a
                # single content match can contribute is 0.45 (fts rank 1), so naming a
                # session beats any transcript that merely repeats the name.
                + candidate.title_score * 0.5
            )
            snippets = candidate.snippets[:3] or [truncate(row["preview"] or row["first_prompt"], 260)]
            results.append(
                SearchResult(
                    provider=row["provider"],
                    session_id=row["session_id"],
                    title=row["title"],
                    cwd=row["cwd"],
                    updated_at=row["updated_at"],
                    git_branch=row["git_branch"],
                    title_source=row["title_source"],
                    score=score,
                    fts_score=candidate.fts_score,
                    fuzzy_score=candidate.fuzzy_score,
                    semantic_score=candidate.semantic_score,
                    recency_score=candidate.recency_score,
                    snippets=dedupe_snippets(snippets),
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:limit]

    def add_fts_candidates(
        self,
        candidates: dict[tuple[str, str], Candidate],
        query: str,
        provider: str | None,
        cwd: str | None,
        ext: list[str] | None = None,
        branch: str | None = None,
    ) -> None:
        rendered = fts_query(query)
        if not rendered:
            return
        filters, params = sql_filters(provider, cwd, table_alias="s", ext=ext, branch=branch)

        message_sql = f"""
            select
                f.provider,
                f.session_id,
                snippet(message_fts, 4, '[', ']', '...', 18) as snippet,
                bm25(message_fts) as rank
            from message_fts f
            join sessions s on s.provider = f.provider and s.session_id = f.session_id
            where message_fts match ?
            {filters}
            order by rank
            limit 200
        """
        session_sql = f"""
            select
                f.provider,
                f.session_id,
                snippet(session_fts, 6, '[', ']', '...', 18) as snippet,
                bm25(session_fts, 0.0, 0.0, 8.0, 1.0, 1.0, 1.0, 1.0) as rank
            from session_fts f
            join sessions s on s.provider = f.provider and s.session_id = f.session_id
            where session_fts match ?
            {filters}
            order by rank
            limit 100
        """
        for sql, boost in ((message_sql, 1.0), (session_sql, 0.9)):
            try:
                rows = self.conn.execute(sql, (rendered, *params)).fetchall()
            except sqlite3.Error:
                continue
            for rank, row in enumerate(rows):
                key = (row["provider"], row["session_id"])
                candidate = candidates.setdefault(key, Candidate(*key))
                candidate.fts_score = max(candidate.fts_score, boost / (rank + 1))
                snippet = truncate(str(row["snippet"] or ""), 280)
                if snippet:
                    candidate.snippets.append(snippet)

    def add_fuzzy_candidates(
        self,
        candidates: dict[tuple[str, str], Candidate],
        query: str,
        provider: str | None,
        cwd: str | None,
        ext: list[str] | None = None,
        branch: str | None = None,
    ) -> None:
        filters, params = sql_filters(provider, cwd, ext=ext, branch=branch)
        rows = self.conn.execute(
            f"""
            select provider, session_id, title, cwd, first_prompt, summary, preview
            from sessions
            where stale = 0
            {filters}
            """,
            params,
        ).fetchall()
        query_lower = query.lower()
        for row in rows:
            target = " ".join(
                [
                    str(row["title"] or ""),
                    str(row["cwd"] or ""),
                    str(row["first_prompt"] or ""),
                    str(row["summary"] or ""),
                    str(row["preview"] or "")[:1200],
                ]
            ).lower()
            score = fuzzy_ratio(query_lower, target)
            if score < 0.46:
                continue
            key = (row["provider"], row["session_id"])
            candidate = candidates.setdefault(key, Candidate(*key))
            candidate.fuzzy_score = max(candidate.fuzzy_score, score)
            if query_lower in target:
                candidate.snippets.append(extract_fuzzy_snippet(target, query_lower))

    def add_semantic_candidates(
        self,
        candidates: dict[tuple[str, str], Candidate],
        query: str,
        provider: str | None,
        cwd: str | None,
        ext: list[str] | None = None,
        branch: str | None = None,
    ) -> None:
        backend_row = self.conn.execute(
            "select backend, dim from embeddings group by backend, dim order by count(*) desc limit 1"
        ).fetchone()
        if backend_row is None:
            return
        backend = str(backend_row["backend"])
        dim = int(backend_row["dim"])
        try:
            embedder = embedder_for_backend(backend, dim)
            query_vector = embedder.embed([query]).vectors[0]
        except Exception:
            return

        filters, params = sql_filters(provider, cwd, table_alias="s", ext=ext, branch=branch)
        rows = self.conn.execute(
            f"""
            select e.provider, e.session_id, e.text, e.embedding, e.dim
            from embeddings e
            join sessions s on s.provider = e.provider and s.session_id = e.session_id
            where e.backend = ?
            {filters}
            """,
            (backend, *params),
        ).fetchall()
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            vector = unpack_vector(row["embedding"], int(row["dim"]))
            score = cosine(query_vector, vector)
            if score > 0.05:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        for rank, (score, row) in enumerate(scored[:120]):
            key = (row["provider"], row["session_id"])
            candidate = candidates.setdefault(key, Candidate(*key))
            normalized = max(0.0, min(1.0, score))
            rank_weight = 1.0 / math.sqrt(rank + 1)
            candidate.semantic_score = max(candidate.semantic_score, normalized * rank_weight)
            candidate.snippets.append(truncate(str(row["text"] or ""), 300))

    def load_sessions(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], sqlite3.Row]:
        sessions: dict[tuple[str, str], sqlite3.Row] = {}
        for provider, session_id in keys:
            row = self.conn.execute(
                "select * from sessions where provider = ? and session_id = ? and stale = 0",
                (provider, session_id),
            ).fetchone()
            if row is not None:
                sessions[(provider, session_id)] = row
        return sessions


def parse_ext_filter(expr: str) -> tuple[str, str, str]:
    """`traycer.epic_title=Payments*` -> (source, key, sql-like pattern).

    Bare `traycer` matches any session carrying that source's annotations.
    """
    selector, _, value = expr.partition("=")
    source, _, key = selector.partition(".")
    return source.strip(), key.strip(), value.strip().replace("*", "%")


def sql_filters(
    provider: str | None,
    cwd: str | None,
    table_alias: str = "sessions",
    ext: list[str] | None = None,
    branch: str | None = None,
) -> tuple[str, list[str]]:
    conditions = [f"{table_alias}.stale = 0"]
    params: list[str] = []
    if provider:
        conditions.append(f"{table_alias}.provider = ?")
        params.append(provider)
    if cwd:
        conditions.append(f"{table_alias}.cwd like ?")
        params.append(f"%{cwd}%")
    for expr in ext or []:
        source, key, value = parse_ext_filter(expr)
        if not source:
            continue
        clause = (
            "exists (select 1 from annotations a where a.provider = "
            f"{table_alias}.provider and a.session_id = {table_alias}.session_id and a.source = ?"
        )
        params.append(source)
        if key:
            clause += " and a.key = ?"
            params.append(key)
        if value:
            clause += " and a.value like ?"
            params.append(f"%{value}%" if "%" not in value else value)
        conditions.append(clause + ")")
    if branch:
        # Matches any branch the session touched, not just the one it started on.
        conditions.append(
            "exists (select 1 from session_branches b where b.provider = "
            f"{table_alias}.provider and b.session_id = {table_alias}.session_id and b.branch like ?)"
        )
        params.append(branch.replace("*", "%") if "*" in branch else f"%{branch}%")
    return " and " + " and ".join(conditions), params


def fuzzy_ratio(query: str, target: str) -> float:
    if not query or not target:
        return 0.0
    if query in target:
        return 1.0
    titleish = target[:1500]
    direct = difflib.SequenceMatcher(None, query, titleish).ratio()
    words = query.split()
    if not words:
        return direct
    hits = sum(1 for word in words if word in target)
    token_score = hits / len(words)
    return max(direct, token_score)


def extract_fuzzy_snippet(target: str, query: str) -> str:
    index = target.find(query)
    if index < 0:
        return truncate(target, 240)
    start = max(0, index - 100)
    end = min(len(target), index + len(query) + 140)
    return truncate(target[start:end], 280)


def recency_score(updated_at: int | None, max_updated: int) -> float:
    if not updated_at or not max_updated:
        return 0.0
    days = max(0.0, (max_updated - updated_at) / 86_400_000)
    return max(0.0, 1.0 - days / 365.0)


def embedder_for_backend(backend: str, dim: int) -> Embedder:
    if backend == HASH_BACKEND:
        return Embedder("hash", dim=dim)
    if backend.startswith("sentence-transformers:"):
        return Embedder("sentence-transformers", model=backend.split(":", 1)[1])
    return Embedder("hash", dim=dim)


def dedupe_snippets(snippets: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for snippet in snippets:
        cleaned = truncate(snippet, 300)
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= 3:
            break
    return result


def format_result_line(index: int, result: SearchResult) -> str:
    when = format_time(result.updated_at)
    return (
        f"{index:>2}. {result.provider:<6} {result.score:0.3f} "
        f"{when:<16} {truncate(result.title, 70)}"
    )
