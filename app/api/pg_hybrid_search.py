"""Evaluation-only PostgreSQL hybrid search with SQL-side authorization."""

from collections.abc import Sequence

import psycopg
from psycopg.rows import dict_row

from app.security.policy import Subject


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE TABLE IF NOT EXISTS retrieval_index_meta (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    source_id text NOT NULL, model_revision text NOT NULL
);
CREATE TABLE IF NOT EXISTS retrieval_documents (
    document_id text PRIMARY KEY, org_id text NOT NULL, title text NOT NULL,
    area text NOT NULL, version text NOT NULL, state text NOT NULL,
    classification integer NOT NULL, visibility text NOT NULL,
    department_ids text[] NOT NULL, allowed_user_ids text[] NOT NULL,
    denied_user_ids text[] NOT NULL, index_event_id bigint NOT NULL DEFAULT 0
);
ALTER TABLE retrieval_documents ADD COLUMN IF NOT EXISTS index_event_id bigint NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS retrieval_segments (
    segment_id bigint PRIMARY KEY, document_id text NOT NULL REFERENCES retrieval_documents(document_id),
    location text NOT NULL, body text NOT NULL, embedding vector(512) NOT NULL
);
CREATE INDEX IF NOT EXISTS retrieval_segments_document ON retrieval_segments(document_id);
CREATE INDEX IF NOT EXISTS retrieval_segments_english ON retrieval_segments
    USING gin (to_tsvector('simple', body));
CREATE INDEX IF NOT EXISTS retrieval_segments_trigram ON retrieval_segments
    USING gin (body gin_trgm_ops);
"""


class PgHybridSearch:
    def __init__(self, dsn: str):
        self.connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self._bound_source: tuple[str, str] | None = None
        if (self.connection.info.dbname != "agent_retrieval"
                or self.connection.info.user != "agent_retrieval"):
            self.connection.close()
            raise ValueError("Shadow search may only use the dedicated agent_retrieval database and role")

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        with self.connection.transaction():
            self.connection.execute(SCHEMA)

    def bind_source(self, source_id: str, model_revision: str, *, rebuild: bool = False) -> None:
        """Refuse a different source or encoder unless the caller requests a rebuild."""
        if not source_id or not model_revision:
            raise ValueError("Source ID and model revision are required")
        with self.connection.transaction():
            self.connection.execute("SELECT pg_advisory_xact_lock(671232319)")
            existing = self.connection.execute("""SELECT source_id, model_revision
                FROM retrieval_index_meta WHERE singleton = true FOR UPDATE""").fetchone()
            if existing is not None and not rebuild and (
                existing["source_id"] != source_id or existing["model_revision"] != model_revision):
                raise ValueError("Shadow index belongs to another source or embedding model")
            if existing is None and not rebuild and self.connection.execute(
                "SELECT 1 FROM retrieval_documents LIMIT 1").fetchone() is not None:
                raise ValueError("Unbound shadow index must be rebuilt before use")
            if rebuild:
                self.connection.execute("TRUNCATE retrieval_segments, retrieval_documents")
            self.connection.execute("""INSERT INTO retrieval_index_meta
                (singleton, source_id, model_revision) VALUES (true, %s, %s)
                ON CONFLICT (singleton) DO UPDATE SET
                    source_id = excluded.source_id, model_revision = excluded.model_revision""",
                (source_id, model_revision))
        self._bound_source = (source_id, model_revision)

    def upsert_document(self, document: dict, segments: Sequence[dict],
                        *, event_id: int | None = None) -> bool:
        """Replace one document and its vectors atomically in the shadow index."""
        if event_id is not None and event_id < 1:
            raise ValueError("Event ID must be positive")
        if self._bound_source is None:
            raise RuntimeError("Shadow index source is not bound")
        payload = {**document, "event_id": event_id}
        with self.connection.transaction():
            current = self.connection.execute("""SELECT source_id, model_revision
                FROM retrieval_index_meta WHERE singleton = true FOR SHARE""").fetchone()
            if current is None or (current["source_id"], current["model_revision"]) != self._bound_source:
                raise RuntimeError("Shadow index source changed; rebind before writing")
            applied = self.connection.execute("""INSERT INTO retrieval_documents
                (document_id, org_id, title, area, version, state, classification,
                 visibility, department_ids, allowed_user_ids, denied_user_ids, index_event_id) VALUES
                (%(document_id)s, %(org_id)s, %(title)s, %(area)s, %(version)s,
                 %(state)s, %(classification)s, %(visibility)s, %(department_ids)s,
                 %(allowed_user_ids)s, %(denied_user_ids)s, COALESCE(%(event_id)s::bigint, 0))
                ON CONFLICT (document_id) DO UPDATE SET
                    org_id = excluded.org_id, title = excluded.title, area = excluded.area,
                    version = excluded.version, state = excluded.state,
                    classification = excluded.classification, visibility = excluded.visibility,
                    department_ids = excluded.department_ids,
                    allowed_user_ids = excluded.allowed_user_ids,
                    denied_user_ids = excluded.denied_user_ids,
                    index_event_id = GREATEST(retrieval_documents.index_event_id,
                                              excluded.index_event_id)
                WHERE %(event_id)s::bigint IS NULL OR
                      retrieval_documents.index_event_id < excluded.index_event_id
                RETURNING document_id""", payload).fetchone()
            if applied is None:
                return False
            self.connection.execute("DELETE FROM retrieval_segments WHERE document_id = %s",
                                    (document["document_id"],))
            with self.connection.cursor() as cursor:
                cursor.executemany("""INSERT INTO retrieval_segments VALUES
                    (%(segment_id)s, %(document_id)s, %(location)s, %(body)s,
                     %(embedding)s::vector)""", segments)
        return True

    def search(self, subject: Subject, question: str, embedding: Sequence[float],
               *, area: str | None = None, limit: int = 3) -> list[dict]:
        if self._bound_source is None:
            raise RuntimeError("Shadow index source is not bound")
        if not subject.active or not subject.user_id or not subject.org_id or limit < 1:
            return []
        if len(embedding) != 512:
            raise ValueError("Expected a 512-dimensional embedding")
        vector = "[" + ",".join(str(float(value)) for value in embedding) + "]"
        # Both ranking branches start from the same authorized candidate set.
        query = """WITH authorized AS MATERIALIZED (
            SELECT s.segment_id, s.document_id, s.location, s.body, s.embedding,
                   d.version, d.title
            FROM retrieval_segments s JOIN retrieval_documents d USING (document_id)
            WHERE d.org_id = %(org_id)s AND d.state = 'published'
              AND d.classification <= %(clearance)s
              AND NOT (%(user_id)s = ANY(d.denied_user_ids))
              AND (%(role)s != 'system_admin' OR %(user_id)s = ANY(d.allowed_user_ids))
              AND (d.visibility = 'organization'
                   OR (d.visibility = 'department' AND d.department_ids && %(departments)s)
                   OR (d.visibility = 'allowlist' AND %(user_id)s = ANY(d.allowed_user_ids)))
              AND (%(area)s::text IS NULL OR d.area = %(area)s::text)
        ), scored AS (
            SELECT *, GREATEST(similarity(title || ' ' || body, %(question)s),
                   ts_rank_cd(to_tsvector('simple', title || ' ' || body),
                              plainto_tsquery('simple', %(question)s))) AS lexical_score,
                   embedding <=> %(embedding)s::vector AS vector_distance
            FROM authorized
        ), ranked AS (
            SELECT *, row_number() OVER (ORDER BY vector_distance, segment_id) AS vector_rank,
                   row_number() OVER (ORDER BY lexical_score DESC, segment_id) AS lexical_rank
            FROM scored
        )
        SELECT segment_id, document_id, version, title, location, body, lexical_score,
               vector_distance, 1.0 / (60 + vector_rank) +
               CASE WHEN lexical_score > 0 THEN 1.0 / (60 + lexical_rank) ELSE 0 END AS score
        FROM ranked ORDER BY score DESC, vector_distance, document_id LIMIT %(limit)s"""
        params = {"org_id": subject.org_id, "user_id": subject.user_id,
                  "clearance": int(subject.clearance), "role": subject.role.value,
                  "departments": sorted(subject.department_ids), "area": area,
                  "embedding": vector, "question": question, "limit": min(limit, 50)}
        return list(self.connection.execute(query, params).fetchall())
