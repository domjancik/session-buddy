from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .embeddings import Embedder, pack_vector
from .models import MessageRecord, SessionRecord, SourceState
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

            create table if not exists source_state (
                source_path text primary key,
                provider text not null,
                session_id text not null,
                file_mtime integer not null,
                file_size integer not null,
                indexed_at integer not null
            );

            create index if not exists idx_sessions_updated on sessions(updated_at desc);
            create index if not exists idx_sessions_provider on sessions(provider);
            create index if not exists idx_sessions_cwd on sessions(cwd);
            create index if not exists idx_messages_session on messages(provider, session_id, idx);
            create index if not exists idx_embeddings_backend on embeddings(backend);
            """
        )
        self.conn.commit()

    def source_state(self, source_path: str) -> SourceState | None:
        row = self.conn.execute(
            "select source_path, file_mtime, file_size, session_id, provider from source_state where source_path = ?",
            (source_path,),
        ).fetchone()
        if row is None:
            return None
        return SourceState(
            source_path=row["source_path"],
            file_mtime=int(row["file_mtime"]),
            file_size=int(row["file_size"]),
            session_id=row["session_id"],
            provider=row["provider"],
        )

    def upsert_session(self, record: SessionRecord, embedder: Embedder | None) -> None:
        with self.conn:
            self.delete_session(record.provider, record.session_id)
            self.conn.execute(
                """
                insert into sessions (
                    provider, session_id, title, cwd, created_at, updated_at, git_branch,
                    source_path, file_mtime, file_size, message_count, first_prompt, summary, preview, stale
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    record.provider,
                    record.session_id,
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
                insert into source_state(source_path, provider, session_id, file_mtime, file_size, indexed_at)
                values (?, ?, ?, ?, ?, ?)
                on conflict(source_path) do update set
                    provider = excluded.provider,
                    session_id = excluded.session_id,
                    file_mtime = excluded.file_mtime,
                    file_size = excluded.file_size,
                    indexed_at = excluded.indexed_at
                """,
                (
                    record.source_path,
                    record.provider,
                    record.session_id,
                    record.file_mtime,
                    record.file_size,
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
