from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .embeddings import Embedder, pack_vector
from .models import AnnotatedTitle, Annotation, MessageRecord, SessionRecord, SourceState
from .text import truncate


class IndexDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            create table if not exists sessions (
                provider text not null,
                session_id text not null,
                title text not null,
                cwd text not null,
                created_at integer,
                updated_at integer,
                git_branch text not null,
                source_path text not null,
                file_mtime integer not null,
                file_size integer not null,
                message_count integer not null,
                first_prompt text not null,
                summary text not null,
                preview text not null,
                stale integer not null default 0,
                primary key (provider, session_id)
            );

            create table if not exists messages (
                id integer primary key autoincrement,
                provider text not null,
                session_id text not null,
                idx integer not null,
                role text not null,
                timestamp integer,
                text text not null
            );

            create virtual table if not exists message_fts using fts5(
                provider unindexed,
                session_id unindexed,
                role unindexed,
                timestamp unindexed,
                text,
                tokenize='unicode61'
            );

            create virtual table if not exists session_fts using fts5(
                provider unindexed,
                session_id unindexed,
                title,
                cwd,
                first_prompt,
                summary,
                preview,
                tokenize='unicode61'
            );

            create table if not exists embeddings (
                provider text not null,
                session_id text not null,
                chunk_id integer not null,
                backend text not null,
                dim integer not null,
                text text not null,
                embedding blob not null,
                primary key (provider, session_id, chunk_id)
            );

            create table if not exists annotations (
                provider text not null,
                session_id text not null,
                source text not null,
                key text not null,
                value text not null,
                primary key (provider, session_id, source, key)
            );

            create table if not exists annotation_groups (
                source text not null,
                group_id text not null,
                title text not null,
                updated_at integer not null default 0,
                member_key text not null default '',
                primary key (source, group_id)
            );

            create table if not exists source_state (
                source_path text primary key,
                provider text not null,
                session_id text not null,
                file_mtime integer not null,
                file_size integer not null,
                metadata_fingerprint text not null default '',
                indexed_at integer not null
            );

            create index if not exists idx_sessions_updated on sessions(updated_at desc);
            create index if not exists idx_sessions_provider on sessions(provider);
            create index if not exists idx_sessions_cwd on sessions(cwd);
            create index if not exists idx_messages_session on messages(provider, session_id, idx);
            create index if not exists idx_embeddings_backend on embeddings(backend);
            create index if not exists idx_annotations_lookup on annotations(source, key, value);
            create index if not exists idx_annotations_session on annotations(provider, session_id);
            """
        )
        self.ensure_column("source_state", "metadata_fingerprint", "text not null default ''")
        # provider_title preserves what the transcript parser derived, so an annotator's
        # claim can be re-resolved (or withdrawn) without re-reading the transcript.
        self.ensure_column("sessions", "provider_title", "text not null default ''")
        self.ensure_column("sessions", "title_source", "text not null default 'provider'")
        self.conn.execute(
            "update sessions set provider_title = title where provider_title = ''"
        )
        self.conn.commit()

    def ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"pragma table_info({table})")}
        if column not in columns:
            self.conn.execute(f"alter table {table} add column {column} {definition}")

    def source_state(self, source_path: str) -> SourceState | None:
        row = self.conn.execute(
            "select source_path, file_mtime, file_size, metadata_fingerprint, session_id, provider from source_state where source_path = ?",
            (source_path,),
        ).fetchone()
        if row is None:
            return None
        return SourceState(
            source_path=row["source_path"],
            file_mtime=int(row["file_mtime"]),
            file_size=int(row["file_size"]),
            metadata_fingerprint=str(row["metadata_fingerprint"] or ""),
            session_id=row["session_id"],
            provider=row["provider"],
        )

    def replace_annotations(self, source: str, annotations: list[Annotation]) -> int:
        """Swap in a source's annotations wholesale.

        Annotations are keyed to a session but stored apart from it, so re-indexing a
        transcript never drops them and an annotator never has to re-read transcripts.
        """
        with self.conn:
            self.conn.execute("delete from annotations where source = ?", (source,))
            self.conn.executemany(
                "insert or replace into annotations (provider, session_id, source, key, value) values (?, ?, ?, ?, ?)",
                [(a.provider, a.session_id, a.source, a.key, a.value) for a in annotations],
            )
        return len(annotations)

    def annotations_for(self, provider: str, session_id: str) -> dict[str, dict[str, str]]:
        rows = self.conn.execute(
            "select source, key, value from annotations where provider = ? and session_id = ? order by source, key",
            (provider, session_id),
        ).fetchall()
        grouped: dict[str, dict[str, str]] = {}
        for row in rows:
            grouped.setdefault(row["source"], {})[row["key"]] = row["value"]
        return grouped

    def apply_annotated_titles(self, titles: list[AnnotatedTitle]) -> int:
        """Promote annotator titles into sessions.title, recording who won in title_source.

        Highest priority wins; ties break on source name so the outcome is deterministic.
        Sessions with no claim fall back to the parser title, which makes withdrawal work.
        """
        best: dict[tuple[str, str], AnnotatedTitle] = {}
        for t in titles:
            if not t.title.strip():
                continue
            key = (t.provider, t.session_id)
            current = best.get(key)
            if current is None or (t.priority, t.source) > (current.priority, current.source):
                best[key] = t
        with self.conn:
            self.conn.execute(
                "update sessions set title = provider_title, title_source = 'provider' where title_source != 'provider'"
            )
            applied = 0
            for (provider, session_id), t in best.items():
                cur = self.conn.execute(
                    "update sessions set title = ?, title_source = ? where provider = ? and session_id = ?",
                    (t.title, t.source, provider, session_id),
                )
                if cur.rowcount:
                    self.conn.execute(
                        "update session_fts set title = ? where provider = ? and session_id = ?",
                        (t.title, provider, session_id),
                    )
                    applied += cur.rowcount
        return applied

    def replace_groups(self, source: str, groups: list[dict[str, str]]) -> int:
        """Persist an annotator's containers so `groups` reads the index, not the live store."""
        with self.conn:
            self.conn.execute("delete from annotation_groups where source = ?", (source,))
            self.conn.executemany(
                "insert or replace into annotation_groups (source, group_id, title, updated_at, member_key) values (?, ?, ?, ?, ?)",
                [
                    (
                        source,
                        str(group.get("id") or ""),
                        str(group.get("title") or ""),
                        int(group.get("updated_at") or 0),
                        str(group.get("key") or ""),
                    )
                    for group in groups
                    if group.get("id")
                ],
            )
        return len(groups)

    def list_groups(self, source: str | None = None) -> list[dict[str, object]]:
        """Groups with the number of sessions actually present in this index.

        The count comes from the annotations rather than the annotator's own tally, so it
        reflects what a search can reach: a group whose sessions were never indexed reads 0.
        """
        params: list[str] = []
        where = ""
        if source:
            where = "where g.source = ?"
            params.append(source)
        rows = self.conn.execute(
            f"""
            select g.source, g.group_id, g.title, g.updated_at,
                   (
                       select count(distinct a.provider || ':' || a.session_id)
                       from annotations a
                       where a.source = g.source
                         and a.key = g.member_key
                         and a.value = g.group_id
                   ) as sessions
            from annotation_groups g
            {where}
            order by g.updated_at desc, g.title
            """,
            params,
        ).fetchall()
        return [
            {
                "source": row["source"],
                "id": row["group_id"],
                "title": row["title"],
                "updated_at": int(row["updated_at"] or 0),
                "sessions": int(row["sessions"] or 0),
            }
            for row in rows
        ]

    def annotation_sources(self) -> list[str]:
        return [row[0] for row in self.conn.execute("select distinct source from annotations order by source")]

    def source_states(self) -> list[SourceState]:
        rows = self.conn.execute(
            "select source_path, file_mtime, file_size, metadata_fingerprint, session_id, provider from source_state"
        ).fetchall()
        return [
            SourceState(
                source_path=row["source_path"],
                file_mtime=int(row["file_mtime"]),
                file_size=int(row["file_size"]),
                metadata_fingerprint=str(row["metadata_fingerprint"] or ""),
                session_id=row["session_id"],
                provider=row["provider"],
            )
            for row in rows
        ]

    def upsert_session(self, record: SessionRecord, embedder: Embedder | None) -> None:
        with self.conn:
            self.delete_session(record.provider, record.session_id)
            self.conn.execute(
                """
                insert into sessions (
                    provider, session_id, title, provider_title, title_source, cwd, created_at, updated_at,
                    git_branch, source_path, file_mtime, file_size, message_count, first_prompt, summary,
                    preview, stale
                ) values (?, ?, ?, ?, 'provider', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    record.provider,
                    record.session_id,
                    record.title,
                    record.title,
                    record.cwd,
                    record.created_at,
                    record.updated_at,
                    record.git_branch,
                    record.source_path,
                    record.file_mtime,
                    record.file_size,
                    record.message_count,
                    record.first_prompt,
                    record.summary,
                    record.preview,
                ),
            )
            self.conn.execute(
                "insert into session_fts(provider, session_id, title, cwd, first_prompt, summary, preview) values (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.provider,
                    record.session_id,
                    record.title,
                    record.cwd,
                    record.first_prompt,
                    record.summary,
                    record.preview,
                ),
            )
            for message in record.messages:
                message_id = self.insert_message(message)
                self.conn.execute(
                    "insert into message_fts(rowid, provider, session_id, role, timestamp, text) values (?, ?, ?, ?, ?, ?)",
                    (
                        message_id,
                        message.provider,
                        message.session_id,
                        message.role,
                        message.timestamp,
                        message.text,
                    ),
                )
            if embedder is not None:
                chunks = build_embedding_chunks(record)
                if chunks:
                    batch = embedder.embed([chunk for _, chunk in chunks])
                    for (chunk_id, text), vector in zip(chunks, batch.vectors):
                        self.conn.execute(
                            """
                            insert into embeddings(provider, session_id, chunk_id, backend, dim, text, embedding)
                            values (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                record.provider,
                                record.session_id,
                                chunk_id,
                                batch.backend,
                                batch.dim,
                                text,
                                pack_vector(vector),
                            ),
                        )
            self.conn.execute(
                """
                insert into source_state(source_path, provider, session_id, file_mtime, file_size, metadata_fingerprint, indexed_at)
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(source_path) do update set
                    provider = excluded.provider,
                    session_id = excluded.session_id,
                    file_mtime = excluded.file_mtime,
                    file_size = excluded.file_size,
                    metadata_fingerprint = excluded.metadata_fingerprint,
                    indexed_at = excluded.indexed_at
                """,
                (
                    record.source_path,
                    record.provider,
                    record.session_id,
                    record.file_mtime,
                    record.file_size,
                    record.metadata_fingerprint,
                    int(time.time() * 1000),
                ),
            )

    def insert_message(self, message: MessageRecord) -> int:
        cursor = self.conn.execute(
            """
            insert into messages(provider, session_id, idx, role, timestamp, text)
            values (?, ?, ?, ?, ?, ?)
            """,
            (
                message.provider,
                message.session_id,
                message.idx,
                message.role,
                message.timestamp,
                message.text,
            ),
        )
        return int(cursor.lastrowid)

    def delete_session(self, provider: str, session_id: str) -> None:
        self.conn.execute("delete from message_fts where provider = ? and session_id = ?", (provider, session_id))
        self.conn.execute("delete from session_fts where provider = ? and session_id = ?", (provider, session_id))
        self.conn.execute("delete from embeddings where provider = ? and session_id = ?", (provider, session_id))
        self.conn.execute("delete from messages where provider = ? and session_id = ?", (provider, session_id))
        self.conn.execute("delete from sessions where provider = ? and session_id = ?", (provider, session_id))

    def mark_seen_sources(self, seen_paths: set[str], prune: bool = False) -> int:
        if not prune:
            return 0
        rows = self.conn.execute("select source_path, provider, session_id from source_state").fetchall()
        removed = 0
        with self.conn:
            for row in rows:
                if row["source_path"] in seen_paths:
                    continue
                self.delete_session(row["provider"], row["session_id"])
                self.conn.execute("delete from source_state where source_path = ?", (row["source_path"],))
                removed += 1
        return removed

    def count_sessions(self) -> int:
        row = self.conn.execute("select count(*) as count from sessions").fetchone()
        return int(row["count"])

    def get_session(self, provider: str, session_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "select * from sessions where provider = ? and session_id = ?",
            (provider, session_id),
        ).fetchone()


def build_embedding_chunks(record: SessionRecord, max_chars: int = 3000) -> list[tuple[int, str]]:
    chunks: list[tuple[int, str]] = []
    header = f"{record.title}\n{record.cwd}\n{record.first_prompt}\n{record.summary}".strip()
    if header:
        chunks.append((0, truncate(header, max_chars)))

    current: list[str] = []
    current_len = 0
    chunk_id = 1
    for message in record.messages:
        rendered = truncate(f"{message.role}: {message.text}", max_chars)
        if current and current_len + len(rendered) > max_chars:
            chunks.append((chunk_id, "\n".join(current)))
            chunk_id += 1
            current = []
            current_len = 0
        current.append(rendered)
        current_len += len(rendered)
    if current:
        chunks.append((chunk_id, "\n".join(current)))
    return chunks
