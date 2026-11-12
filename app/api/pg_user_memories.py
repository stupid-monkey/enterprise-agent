"""PostgreSQL long-term memory store, separate from the retrieval shadow DB."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.api.user_memories import MEMORY_KINDS, _task_continues, _task_topic
from app.security.policy import Subject


SCHEMA_VERSION = 1
QUEUE_SCHEMA_VERSION = 6
MAX_JOB_ATTEMPTS = 3
FAILURE_CODES = frozenset(("model_timeout", "model_unavailable", "model_rate_limited",
                           "model_auth_failed", "model_invalid", "memory_write_failed"))
FACT_LIFETIMES = {"role": 180, "task": 30, "answer_preference": 365}
SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_schema_version (
    version integer PRIMARY KEY CHECK (version = 1)
);
INSERT INTO memory_schema_version(version) VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS memory_fact (
    org_id text NOT NULL, owner_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('role', 'task', 'answer_preference')),
    value text NOT NULL CHECK (length(value) BETWEEN 1 AND 160),
    origin text NOT NULL CHECK (origin IN ('manual', 'auto')),
    source_conversation_id text,
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
    expires_at timestamptz, version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    PRIMARY KEY (org_id, owner_id, kind),
    CHECK (origin <> 'auto' OR source_conversation_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS memory_fact_source
    ON memory_fact(org_id, owner_id, source_conversation_id) WHERE origin = 'auto';
CREATE TABLE IF NOT EXISTS memory_setting (
    org_id text NOT NULL, owner_id text NOT NULL, auto_suggest boolean NOT NULL,
    PRIMARY KEY (org_id, owner_id)
);
CREATE TABLE IF NOT EXISTS memory_suppression (
    org_id text NOT NULL, owner_id text NOT NULL, fingerprint text NOT NULL,
    PRIMARY KEY (org_id, owner_id, fingerprint)
);
"""

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_queue_schema_version (
    version integer PRIMARY KEY CHECK (version = 1)
);
CREATE TABLE IF NOT EXISTS memory_job (
    id text PRIMARY KEY, org_id text NOT NULL, owner_id text NOT NULL,
    source_conversation_id text NOT NULL, question text NOT NULL,
    context_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    state text NOT NULL CHECK (state IN ('queued', 'running')),
    created_at timestamptz NOT NULL, available_at timestamptz NOT NULL,
    lease_token text, lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3)
);
CREATE INDEX IF NOT EXISTS memory_job_ready
    ON memory_job(state, available_at, created_at);
CREATE TABLE IF NOT EXISTS memory_job_effect (
    job_id text NOT NULL REFERENCES memory_job(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('role', 'task', 'answer_preference')),
    PRIMARY KEY (job_id, kind)
);
CREATE TABLE IF NOT EXISTS memory_job_failure (
    job_id text PRIMARY KEY, org_id text NOT NULL, owner_id text NOT NULL,
    source_conversation_id text NOT NULL, reason_code text NOT NULL,
    attempts integer NOT NULL, failed_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_job_failure_owner
    ON memory_job_failure(org_id, owner_id, failed_at DESC);
"""

QUEUE_V2 = """
CREATE TABLE IF NOT EXISTS memory_fact_guard (
    org_id text NOT NULL, owner_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('role', 'task', 'answer_preference')),
    cutoff_at timestamptz NOT NULL,
    PRIMARY KEY (org_id, owner_id, kind)
);
"""

QUEUE_V3 = """
CREATE TABLE IF NOT EXISTS memory_source (
    org_id text NOT NULL, owner_id text NOT NULL, id text NOT NULL,
    state text NOT NULL CHECK (state IN ('active', 'deleted')),
    created_at timestamptz NOT NULL, deleted_at timestamptz,
    PRIMARY KEY (org_id, owner_id, id)
);
"""

QUEUE_V4 = """
CREATE TABLE IF NOT EXISTS memory_suggestion (
    id text PRIMARY KEY, org_id text NOT NULL, owner_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('role', 'task', 'answer_preference')),
    value text NOT NULL CHECK (length(value) BETWEEN 1 AND 160),
    source_conversation_id text NOT NULL, source_text text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (org_id, owner_id, kind, value)
);
CREATE INDEX IF NOT EXISTS memory_suggestion_owner
    ON memory_suggestion(org_id, owner_id, created_at DESC);
"""

QUEUE_V5 = """
ALTER TABLE memory_source DROP CONSTRAINT IF EXISTS memory_source_state_check;
ALTER TABLE memory_source ADD CONSTRAINT memory_source_state_check
    CHECK (state IN ('pending', 'active', 'deleted'));
"""

QUEUE_V6 = """
CREATE TABLE IF NOT EXISTS memory_enqueue_receipt (
    event_id text PRIMARY KEY, org_id text NOT NULL, owner_id text NOT NULL,
    source_conversation_id text NOT NULL, created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_enqueue_receipt_source
    ON memory_enqueue_receipt(org_id, owner_id, source_conversation_id);
"""


class PgUserMemoryRepository:
    """PostgreSQL facts, sources and worker queue for the memory backend."""

    def __init__(self, dsn: str, *, schema: str = "memory",
                 default_auto_suggest: bool = False):
        self.connection = psycopg.connect(
            dsn, row_factory=dict_row, autocommit=True, connect_timeout=3)
        self.schema = schema
        self.default_auto_suggest = default_auto_suggest
        self.connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))

    def create_schema(self) -> None:
        with self.connection.transaction():
            self.connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(self.schema)))
            self.connection.execute(sql.SQL("SET search_path TO {}").format(
                sql.Identifier(self.schema)))
            self.connection.execute(SCHEMA)
            versions = [row["version"] for row in self.connection.execute(
                "SELECT version FROM memory_schema_version")]
            if versions != [SCHEMA_VERSION]:
                raise RuntimeError("UNSUPPORTED_MEMORY_SCHEMA_VERSION")
            self.connection.execute(QUEUE_SCHEMA)
            self.connection.execute("""INSERT INTO memory_queue_schema_version(version)
                SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM memory_queue_schema_version)""")
            queue_versions = [row["version"] for row in self.connection.execute(
                "SELECT version FROM memory_queue_schema_version")]
            if queue_versions == [1]:
                self.connection.execute(QUEUE_V2)
                self.connection.execute("""ALTER TABLE memory_queue_schema_version
                    DROP CONSTRAINT IF EXISTS memory_queue_schema_version_version_check""")
                self.connection.execute("UPDATE memory_queue_schema_version SET version = 2")
                self.connection.execute("""ALTER TABLE memory_queue_schema_version
                    ADD CONSTRAINT memory_queue_schema_version_version_check CHECK (version = 2)""")
                queue_versions = [2]
            if queue_versions == [2]:
                self.connection.execute(QUEUE_V3)
                self.connection.execute("""ALTER TABLE memory_job ADD COLUMN IF NOT EXISTS
                    source_validated boolean NOT NULL DEFAULT false""")
                self.connection.execute("""ALTER TABLE memory_queue_schema_version
                    DROP CONSTRAINT IF EXISTS memory_queue_schema_version_version_check""")
                self.connection.execute("UPDATE memory_queue_schema_version SET version = 3")
                self.connection.execute("""ALTER TABLE memory_queue_schema_version
                    ADD CONSTRAINT memory_queue_schema_version_version_check CHECK (version = 3)""")
                queue_versions = [3]
            if queue_versions == [3]:
                self.connection.execute(QUEUE_V4)
                self.connection.execute("""ALTER TABLE memory_queue_schema_version
                    DROP CONSTRAINT IF EXISTS memory_queue_schema_version_version_check""")
                self.connection.execute("UPDATE memory_queue_schema_version SET version = 4")
                self.connection.execute("""ALTER TABLE memory_queue_schema_version
                    ADD CONSTRAINT memory_queue_schema_version_version_check CHECK (version = 4)""")
                queue_versions = [4]
            if queue_versions == [4]:
                self.connection.execute(QUEUE_V5)
                self.connection.execute("""ALTER TABLE memory_queue_schema_version
                    DROP CONSTRAINT IF EXISTS memory_queue_schema_version_version_check""")
                self.connection.execute("UPDATE memory_queue_schema_version SET version = 5")
                self.connection.execute("""ALTER TABLE memory_queue_schema_version
                    ADD CONSTRAINT memory_queue_schema_version_version_check CHECK (version = 5)""")
                queue_versions = [5]
            if queue_versions == [5]:
                self.connection.execute(QUEUE_V6)
                self.connection.execute("""ALTER TABLE memory_queue_schema_version
                    DROP CONSTRAINT IF EXISTS memory_queue_schema_version_version_check""")
                self.connection.execute("UPDATE memory_queue_schema_version SET version = 6")
                self.connection.execute("""ALTER TABLE memory_queue_schema_version
                    ADD CONSTRAINT memory_queue_schema_version_version_check CHECK (version = 6)""")
                queue_versions = [6]
            if queue_versions != [QUEUE_SCHEMA_VERSION]:
                raise RuntimeError("UNSUPPORTED_MEMORY_QUEUE_SCHEMA_VERSION")
            self.connection.execute(QUEUE_V2)
            self.connection.execute(QUEUE_V3)
            self.connection.execute(QUEUE_V4)
            self.connection.execute(QUEUE_V6)

    def check_schema(self) -> None:
        """Fail startup on an unprepared or unsupported database without DDL."""
        try:
            fact = self.connection.execute(
                "SELECT version FROM memory_schema_version").fetchall()
            queue = self.connection.execute(
                "SELECT version FROM memory_queue_schema_version").fetchall()
        except psycopg.Error as exc:
            raise RuntimeError("POSTGRES_MEMORY_SCHEMA_NOT_READY") from exc
        if ([row["version"] for row in fact] != [SCHEMA_VERSION]
                or [row["version"] for row in queue] != [QUEUE_SCHEMA_VERSION]):
            raise RuntimeError("UNSUPPORTED_POSTGRES_MEMORY_SCHEMA_VERSION")

    def verify_import_manifest(self, memory_sha256: str,
                               conversation_sha256: str) -> None:
        """Require the exact reviewed offline snapshots before product cutover."""
        for value in (memory_sha256, conversation_sha256):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("MEMORY_CUTOVER_HASH_REQUIRED")
        try:
            row = self.connection.execute("""SELECT memory_sha256,
                conversation_sha256 FROM memory_import_manifest WHERE id = 1""").fetchone()
        except psycopg.Error as exc:
            raise RuntimeError("MEMORY_IMPORT_MANIFEST_MISSING") from exc
        if (row is None or row["memory_sha256"] != memory_sha256
                or row["conversation_sha256"] != conversation_sha256):
            raise RuntimeError("MEMORY_IMPORT_MANIFEST_MISMATCH")

    def _guard_fact(self, org_id: str, owner_id: str, kind: str) -> None:
        """Block jobs created before a manual edit, deletion or explicit retraction."""
        self.connection.execute("""INSERT INTO memory_fact_guard
            (org_id, owner_id, kind, cutoff_at) VALUES (%s, %s, %s, clock_timestamp())
            ON CONFLICT (org_id, owner_id, kind) DO UPDATE SET
                cutoff_at = GREATEST(memory_fact_guard.cutoff_at, excluded.cutoff_at)""",
            (org_id, owner_id, kind))

    def _lock_guard(self, org_id: str, owner_id: str, kind: str) -> datetime:
        """Lock even a previously absent guard row before touching the fact row."""
        self.connection.execute("""INSERT INTO memory_fact_guard
            (org_id, owner_id, kind, cutoff_at)
            VALUES (%s, %s, %s, 'epoch'::timestamptz) ON CONFLICT DO NOTHING""",
            (org_id, owner_id, kind))
        return self.connection.execute("""SELECT cutoff_at FROM memory_fact_guard
            WHERE org_id = %s AND owner_id = %s AND kind = %s FOR UPDATE""",
            (org_id, owner_id, kind)).fetchone()["cutoff_at"]

    @staticmethod
    def _api_row(row: dict) -> dict:
        """Match the SQLite repository's ISO-8601 response field types."""
        item = dict(row)
        for key in ("created_at", "updated_at", "expires_at", "failed_at"):
            if isinstance(item.get(key), datetime):
                item[key] = item[key].isoformat(timespec="microseconds")
        return item

    def list_for(self, subject: Subject) -> list[dict]:
        rows = self.connection.execute("""SELECT kind, value, origin, source_conversation_id,
            created_at, updated_at, expires_at, version FROM memory_fact
            WHERE org_id = %s AND owner_id = %s
              AND (expires_at IS NULL OR expires_at > now()) ORDER BY kind""",
            (subject.org_id, subject.user_id)).fetchall()
        return [self._api_row(row) for row in rows]

    def upsert(self, subject: Subject, kind: str, value: str) -> dict:
        if kind not in MEMORY_KINDS or not value.strip() or len(value.strip()) > 160:
            raise ValueError("INVALID_MEMORY_FACT")
        now = datetime.now(timezone.utc)
        with self.connection.transaction():
            self._guard_fact(subject.org_id, subject.user_id, kind)
            row = self.connection.execute("""INSERT INTO memory_fact
                (org_id, owner_id, kind, value, origin, created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'manual', %s, %s)
                ON CONFLICT (org_id, owner_id, kind) DO UPDATE SET
                    value = excluded.value, origin = 'manual',
                    source_conversation_id = NULL, expires_at = NULL,
                    updated_at = excluded.updated_at, version = memory_fact.version + 1
                RETURNING kind, value, origin, source_conversation_id,
                          created_at, updated_at, expires_at, version""",
                (subject.org_id, subject.user_id, kind, value.strip(), now, now)).fetchone()
        return self._api_row(row)

    def delete(self, subject: Subject, kind: str) -> None:
        if kind not in MEMORY_KINDS:
            raise ValueError("INVALID_MEMORY_KIND")
        with self.connection.transaction():
            self._guard_fact(subject.org_id, subject.user_id, kind)
            row = self.connection.execute("""SELECT value FROM memory_fact
                WHERE org_id = %s AND owner_id = %s AND kind = %s FOR UPDATE""",
                (subject.org_id, subject.user_id, kind)).fetchone()
            if row:
                fingerprint = hashlib.sha256(
                    f"{kind}\0{row['value'].strip().casefold()}".encode()).hexdigest()
                self.connection.execute("""INSERT INTO memory_suppression
                    (org_id, owner_id, fingerprint) VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING""",
                    (subject.org_id, subject.user_id, fingerprint))
            self.connection.execute("""DELETE FROM memory_fact
                WHERE org_id = %s AND owner_id = %s AND kind = %s""",
                (subject.org_id, subject.user_id, kind))

    def register_source(self, subject: Subject, conversation_id: str) -> bool:
        """Lab-only source registration; the future API must verify the real conversation."""
        if not conversation_id:
            return False
        with self.connection.transaction():
            self.connection.execute("""INSERT INTO memory_source
                (org_id, owner_id, id, state, created_at)
                VALUES (%s, %s, %s, 'active', now()) ON CONFLICT DO NOTHING""",
                (subject.org_id, subject.user_id, conversation_id))
            row = self.connection.execute("""SELECT state FROM memory_source
                WHERE org_id = %s AND owner_id = %s AND id = %s FOR UPDATE""",
                (subject.org_id, subject.user_id, conversation_id)).fetchone()
            return bool(row and row["state"] == "active")

    def prepare_source(self, subject: Subject, conversation_id: str) -> bool:
        """Create an inert source until the authoritative SQLite owner is rechecked."""
        if not conversation_id:
            return False
        with self.connection.transaction():
            self.connection.execute("""INSERT INTO memory_source
                (org_id, owner_id, id, state, created_at)
                VALUES (%s, %s, %s, 'pending', now()) ON CONFLICT DO NOTHING""",
                (subject.org_id, subject.user_id, conversation_id))
            row = self.connection.execute("""SELECT state FROM memory_source
                WHERE org_id = %s AND owner_id = %s AND id = %s FOR UPDATE""",
                (subject.org_id, subject.user_id, conversation_id)).fetchone()
            return bool(row and row["state"] in ("pending", "active"))

    def activate_source(self, subject: Subject, conversation_id: str) -> bool:
        """Activate only a prepared source; deletion tombstones cannot reopen."""
        with self.connection.transaction():
            row = self.connection.execute("""SELECT state FROM memory_source
                WHERE org_id = %s AND owner_id = %s AND id = %s FOR UPDATE""",
                (subject.org_id, subject.user_id, conversation_id)).fetchone()
            if row is None or row["state"] == "deleted":
                return False
            if row["state"] == "pending":
                self.connection.execute("""UPDATE memory_source SET state = 'active'
                    WHERE org_id = %s AND owner_id = %s AND id = %s""",
                    (subject.org_id, subject.user_id, conversation_id))
            return True

    def _delete_source(self, subject: Subject, conversation_id: str,
                       *, allow_unregistered: bool = False) -> tuple[bool, int]:
        with self.connection.transaction():
            scope = (subject.org_id, subject.user_id, conversation_id)
            source = self.connection.execute("""SELECT state FROM memory_source
                WHERE org_id = %s AND owner_id = %s AND id = %s FOR UPDATE""",
                scope).fetchone()
            if source and source["state"] == "deleted":
                return False, 0
            if source:
                self.connection.execute("""UPDATE memory_source SET state = 'deleted',
                    deleted_at = clock_timestamp() WHERE org_id = %s AND owner_id = %s
                    AND id = %s""", scope)
            elif allow_unregistered:
                self.connection.execute("""INSERT INTO memory_source
                    (org_id, owner_id, id, state, created_at, deleted_at)
                    VALUES (%s, %s, %s, 'deleted', now(), clock_timestamp())""", scope)
            else:
                return False, 0
            self.connection.execute("""DELETE FROM memory_job WHERE org_id = %s
                AND owner_id = %s AND source_conversation_id = %s""", scope)
            self.connection.execute("""DELETE FROM memory_job_failure WHERE org_id = %s
                AND owner_id = %s AND source_conversation_id = %s""", scope)
            self.connection.execute("""DELETE FROM memory_enqueue_receipt WHERE org_id = %s
                AND owner_id = %s AND source_conversation_id = %s""", scope)
            self.connection.execute("""DELETE FROM memory_suggestion WHERE org_id = %s
                AND owner_id = %s AND source_conversation_id = %s""", scope)
            kinds = self.connection.execute("""SELECT kind FROM memory_fact
                WHERE org_id = %s AND owner_id = %s AND origin = 'auto'
                AND source_conversation_id = %s""", scope).fetchall()
            for item in kinds:
                self._guard_fact(subject.org_id, subject.user_id, item["kind"])
            result = self.connection.execute("""DELETE FROM memory_fact
                WHERE org_id = %s AND owner_id = %s AND origin = 'auto'
                  AND source_conversation_id = %s""",
                scope)
        return True, result.rowcount

    def delete_source(self, subject: Subject, conversation_id: str) -> bool:
        return self._delete_source(subject, conversation_id)[0]

    def discard_conversation_auto(self, subject: Subject, conversation_id: str) -> int:
        return self._delete_source(subject, conversation_id, allow_unregistered=True)[1]

    def discard_conversation_suggestions(self, subject: Subject, conversation_id: str) -> None:
        """Product-shaped deletion hook; tombstone the source before another write."""
        self.discard_conversation_auto(subject, conversation_id)

    def auto_suggest_enabled(self, subject: Subject) -> bool:
        row = self.connection.execute("""SELECT auto_suggest FROM memory_setting
            WHERE org_id = %s AND owner_id = %s""",
            (subject.org_id, subject.user_id)).fetchone()
        return bool(row["auto_suggest"]) if row else self.default_auto_suggest

    def set_auto_suggest(self, subject: Subject, enabled: bool) -> None:
        with self.connection.transaction():
            self.connection.execute("""INSERT INTO memory_setting
                (org_id, owner_id, auto_suggest) VALUES (%s, %s, %s)
                ON CONFLICT (org_id, owner_id) DO UPDATE SET
                auto_suggest = excluded.auto_suggest""",
                (subject.org_id, subject.user_id, enabled))
            if not enabled:
                self.connection.execute("""DELETE FROM memory_suggestion
                    WHERE org_id = %s AND owner_id = %s""",
                    (subject.org_id, subject.user_id))
                self.connection.execute("""DELETE FROM memory_job
                    WHERE org_id = %s AND owner_id = %s""",
                    (subject.org_id, subject.user_id))
                self.connection.execute("""DELETE FROM memory_job_failure
                    WHERE org_id = %s AND owner_id = %s""",
                    (subject.org_id, subject.user_id))

    def list_suggestions(self, subject: Subject) -> list[dict]:
        rows = self.connection.execute("""SELECT id, kind, value, source_conversation_id,
            source_text, created_at FROM memory_suggestion
            WHERE org_id = %s AND owner_id = %s
            ORDER BY created_at DESC, id DESC LIMIT 30""",
            (subject.org_id, subject.user_id)).fetchall()
        return [self._api_row(row) for row in rows]

    def propose(self, subject: Subject, kind: str, value: str,
                conversation_id: str, source_text: str,
                *, job_lease: tuple[str, str] | None = None) -> None:
        """Persist a quoted candidate only from this owner's live leased job."""
        if (kind not in MEMORY_KINDS or not isinstance(value, str) or not value
                or len(value) > 160 or not isinstance(source_text, str)
                or value not in source_text or not conversation_id
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or not isinstance(job_lease, tuple) or len(job_lease) != 2
                or not all(isinstance(part, str) for part in job_lease)):
            return
        job_id, token = job_lease
        with self.connection.transaction():
            source = self.connection.execute("""SELECT state FROM memory_source
                WHERE org_id = %s AND owner_id = %s AND id = %s FOR UPDATE""",
                (subject.org_id, subject.user_id, conversation_id)).fetchone()
            if not source or source["state"] != "active":
                return
            setting = self.connection.execute("""SELECT auto_suggest FROM memory_setting
                WHERE org_id = %s AND owner_id = %s FOR UPDATE""",
                (subject.org_id, subject.user_id)).fetchone()
            if not setting or not setting["auto_suggest"]:
                return
            job = self.connection.execute("""SELECT 1 FROM memory_job WHERE id = %s
                AND org_id = %s AND owner_id = %s AND source_conversation_id = %s
                AND question = %s AND source_validated AND state = 'running'
                AND lease_token = %s AND lease_until > clock_timestamp() FOR UPDATE""",
                (job_id, subject.org_id, subject.user_id, conversation_id,
                 source_text, token)).fetchone()
            if not job:
                return
            if self.connection.execute("""SELECT 1 FROM memory_fact
                WHERE org_id = %s AND owner_id = %s AND kind = %s AND value = %s
                AND (expires_at IS NULL OR expires_at > now())""",
                (subject.org_id, subject.user_id, kind, value)).fetchone():
                return
            fingerprint = hashlib.sha256(
                f"{kind}\0{value.strip().casefold()}".encode()).hexdigest()
            if self.connection.execute("""SELECT 1 FROM memory_suppression
                WHERE org_id = %s AND owner_id = %s AND fingerprint = %s""",
                (subject.org_id, subject.user_id, fingerprint)).fetchone():
                return
            count = self.connection.execute("""SELECT count(*) AS n FROM memory_suggestion
                WHERE org_id = %s AND owner_id = %s""",
                (subject.org_id, subject.user_id)).fetchone()["n"]
            if count >= 30:
                return
            start = max(0, source_text.find(value) - 40)
            excerpt = source_text[start:start + 220]
            self.connection.execute("""INSERT INTO memory_suggestion
                (id, org_id, owner_id, kind, value, source_conversation_id,
                 source_text, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (org_id, owner_id, kind, value) DO NOTHING""",
                (uuid4().hex, subject.org_id, subject.user_id, kind, value,
                 conversation_id, excerpt))

    def reject_suggestion(self, subject: Subject, suggestion_id: str) -> bool:
        with self.connection.transaction():
            row = self.connection.execute("""SELECT kind, value FROM memory_suggestion
                WHERE id = %s AND org_id = %s AND owner_id = %s FOR UPDATE""",
                (suggestion_id, subject.org_id, subject.user_id)).fetchone()
            if row is None:
                return False
            fingerprint = hashlib.sha256(
                f"{row['kind']}\0{row['value'].strip().casefold()}".encode()).hexdigest()
            self.connection.execute("""INSERT INTO memory_suppression
                (org_id, owner_id, fingerprint) VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING""",
                (subject.org_id, subject.user_id, fingerprint))
            self.connection.execute("DELETE FROM memory_suggestion WHERE id = %s",
                                    (suggestion_id,))
            return True

    def accept_suggestion(self, subject: Subject, suggestion_id: str,
                          value: str) -> dict | None:
        if (not isinstance(value, str) or not value.strip() or len(value.strip()) > 160
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError("INVALID_MEMORY_FACT")
        with self.connection.transaction():
            row = self.connection.execute("""SELECT kind, value FROM memory_suggestion
                WHERE id = %s AND org_id = %s AND owner_id = %s FOR UPDATE""",
                (suggestion_id, subject.org_id, subject.user_id)).fetchone()
            if row is None:
                return None
            fingerprint = hashlib.sha256(
                f"{row['kind']}\0{row['value'].strip().casefold()}".encode()).hexdigest()
            self.connection.execute("""INSERT INTO memory_suppression
                (org_id, owner_id, fingerprint) VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING""",
                (subject.org_id, subject.user_id, fingerprint))
            fact = self.upsert(subject, row["kind"], value)
            self.connection.execute("DELETE FROM memory_suggestion WHERE id = %s",
                                    (suggestion_id,))
            return fact

    def enqueue_suggestion(self, subject: Subject, question: str,
                           conversation_id: str, context: tuple[str, ...] = (),
                           *, include_ordinary: bool = True,
                           event_id: str | None = None) -> str | None:
        from app.api.memory_suggestions import eligible_auto_memory_text, eligible_user_text

        eligible = eligible_auto_memory_text if include_ordinary else eligible_user_text
        if (not conversation_id or not eligible(question)
                or (event_id is not None and
                    (len(event_id) != 32 or any(char not in "0123456789abcdef"
                                                for char in event_id)))):
            return None
        safe_context = ([item[:500] for item in context[-3:]
                         if isinstance(item, str) and eligible_auto_memory_text(item)]
                        if include_ordinary else [])
        with self.connection.transaction():
            source = self.connection.execute("""SELECT state FROM memory_source
                WHERE org_id = %s AND owner_id = %s AND id = %s FOR UPDATE""",
                (subject.org_id, subject.user_id, conversation_id)).fetchone()
            if not source or source["state"] != "active":
                return None
            if event_id is not None:
                receipt = self.connection.execute("""SELECT org_id, owner_id,
                    source_conversation_id FROM memory_enqueue_receipt
                    WHERE event_id = %s FOR UPDATE""", (event_id,)).fetchone()
                if receipt is not None:
                    if (receipt["org_id"] != subject.org_id
                            or receipt["owner_id"] != subject.user_id
                            or receipt["source_conversation_id"] != conversation_id):
                        raise ValueError("MEMORY_EVENT_ID_COLLISION")
                    return event_id
            self.connection.execute("""INSERT INTO memory_setting
                (org_id, owner_id, auto_suggest) VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING""",
                (subject.org_id, subject.user_id, self.default_auto_suggest))
            setting = self.connection.execute("""SELECT auto_suggest FROM memory_setting
                WHERE org_id = %s AND owner_id = %s FOR UPDATE""",
                (subject.org_id, subject.user_id)).fetchone()
            if not setting or not setting["auto_suggest"]:
                return None
            count = self.connection.execute("""SELECT count(*) AS n FROM memory_job
                WHERE org_id = %s AND owner_id = %s""",
                (subject.org_id, subject.user_id)).fetchone()["n"]
            if count >= 30:
                return None
            if event_id is not None:
                self.connection.execute("""INSERT INTO memory_enqueue_receipt
                    (event_id, org_id, owner_id, source_conversation_id, created_at)
                    VALUES (%s, %s, %s, %s, now())""",
                    (event_id, subject.org_id, subject.user_id, conversation_id))
            job_id = uuid4().hex
            self.connection.execute("""INSERT INTO memory_job
                (id, org_id, owner_id, source_conversation_id, question,
                 context_json, state, created_at, available_at, source_validated)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'queued', now(), now(), true)""",
                (job_id, subject.org_id, subject.user_id, conversation_id, question,
                 json.dumps(safe_context, ensure_ascii=False)))
            return job_id

    def _recover_jobs(self) -> None:
        """Expire raw text after 24h and reclaim only expired leases."""
        self.connection.execute("""DELETE FROM memory_job_failure
            WHERE failed_at < now() - interval '7 days'""")
        self.connection.execute("""INSERT INTO memory_job_failure
            (job_id, org_id, owner_id, source_conversation_id, reason_code, attempts, failed_at)
            SELECT id, org_id, owner_id, source_conversation_id, 'job_expired', attempts, now()
            FROM memory_job WHERE created_at < now() - interval '24 hours'
            ON CONFLICT (job_id) DO NOTHING""")
        self.connection.execute("""DELETE FROM memory_job
            WHERE created_at < now() - interval '24 hours'""")
        self.connection.execute("""INSERT INTO memory_job_failure
            (job_id, org_id, owner_id, source_conversation_id, reason_code, attempts, failed_at)
            SELECT id, org_id, owner_id, source_conversation_id, 'lease_exhausted', attempts, now()
            FROM memory_job WHERE state = 'running' AND
                (lease_until IS NULL OR lease_until <= now()) AND attempts >= %s
            ON CONFLICT (job_id) DO NOTHING""", (MAX_JOB_ATTEMPTS,))
        self.connection.execute("""DELETE FROM memory_job WHERE state = 'running' AND
            (lease_until IS NULL OR lease_until <= now()) AND attempts >= %s""",
            (MAX_JOB_ATTEMPTS,))
        self.connection.execute("""UPDATE memory_job SET state = 'queued', lease_token = NULL,
            lease_until = NULL, available_at = now() WHERE state = 'running' AND
            (lease_until IS NULL OR lease_until <= now())""")

    def recover_suggestion_jobs(self) -> None:
        with self.connection.transaction():
            self._recover_jobs()

    def claim_suggestion_job(self) -> dict | None:
        with self.connection.transaction():
            self._recover_jobs()
            token = uuid4().hex
            row = self.connection.execute("""WITH ready AS (
                SELECT job.id FROM memory_job AS job JOIN memory_source AS source
                  ON source.org_id = job.org_id AND source.owner_id = job.owner_id
                 AND source.id = job.source_conversation_id
                WHERE job.state = 'queued' AND job.source_validated
                  AND source.state = 'active' AND job.available_at <= now()
                ORDER BY job.available_at, job.created_at, job.id
                FOR UPDATE OF job SKIP LOCKED LIMIT 1
            ) UPDATE memory_job AS job SET state = 'running', lease_token = %s,
                lease_until = now() + interval '90 seconds', attempts = attempts + 1
                FROM ready WHERE job.id = ready.id
                RETURNING job.id, job.org_id, job.owner_id, job.source_conversation_id,
                          job.question, job.context_json, job.attempts, job.lease_token""",
                (token,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["context"] = tuple(result.pop("context_json"))
        return result

    def next_suggestion_lease_delay(self) -> float | None:
        row = self.connection.execute("""SELECT min(next_at) AS next_at FROM (
            SELECT job.lease_until AS next_at FROM memory_job AS job
                JOIN memory_source AS source ON source.org_id = job.org_id
                  AND source.owner_id = job.owner_id
                  AND source.id = job.source_conversation_id
                WHERE job.state = 'running' AND job.lease_until IS NOT NULL
                  AND job.source_validated AND source.state = 'active'
            UNION ALL
            SELECT job.available_at AS next_at FROM memory_job AS job
                JOIN memory_source AS source ON source.org_id = job.org_id
                  AND source.owner_id = job.owner_id
                  AND source.id = job.source_conversation_id
                WHERE job.state = 'queued' AND job.source_validated
                  AND source.state = 'active') AS waits""").fetchone()
        if row["next_at"] is None:
            return None
        return max(0.0, (row["next_at"] - datetime.now(timezone.utc)).total_seconds())

    def job_lease_current(self, job_id: str, lease_token: str) -> bool:
        return self.connection.execute("""SELECT 1 FROM memory_job AS job
            JOIN memory_source AS source ON source.org_id = job.org_id
              AND source.owner_id = job.owner_id AND source.id = job.source_conversation_id
            WHERE job.id = %s AND job.source_validated AND source.state = 'active'
              AND job.state = 'running' AND job.lease_token = %s
              AND job.lease_until > clock_timestamp()""",
            (job_id, lease_token)).fetchone() is not None

    def apply_auto_fact(self, job_id: str, lease_token: str,
                        kind: str, value: str) -> dict | None:
        """Commit one quoted fact only while its claim and owner setting remain valid."""
        from app.api.memory_suggestions import eligible_auto_memory_text

        if (kind not in MEMORY_KINDS or not isinstance(value, str) or not value.strip()
                or len(value.strip()) > 160
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return None
        value = value.strip()
        with self.connection.transaction():
            identity = self.connection.execute("""SELECT org_id, owner_id,
                source_conversation_id, source_validated FROM memory_job
                WHERE id = %s""", (job_id,)).fetchone()
            if identity is None or not identity["source_validated"]:
                return None
            source = self.connection.execute("""SELECT state FROM memory_source
                WHERE org_id = %s AND owner_id = %s AND id = %s FOR UPDATE""",
                (identity["org_id"], identity["owner_id"],
                 identity["source_conversation_id"])).fetchone()
            if not source or source["state"] != "active":
                return None
            setting = self.connection.execute("""SELECT auto_suggest FROM memory_setting
                WHERE org_id = %s AND owner_id = %s FOR UPDATE""",
                (identity["org_id"], identity["owner_id"])).fetchone()
            if not setting or not setting["auto_suggest"]:
                return None
            job = self.connection.execute("""SELECT *, lease_until > clock_timestamp() AS lease_active
                FROM memory_job WHERE id = %s
                FOR UPDATE""", (job_id,)).fetchone()
            if (not job or job["state"] != "running" or job["lease_token"] != lease_token
                    or not job["lease_active"]
                    or value not in job["question"]
                    or not eligible_auto_memory_text(job["question"])):
                return None
            cutoff = self._lock_guard(job["org_id"], job["owner_id"], kind)
            if job["created_at"] <= cutoff:
                return None
            fingerprint = hashlib.sha256(f"{kind}\0{value.casefold()}".encode()).hexdigest()
            if self.connection.execute("""SELECT 1 FROM memory_suppression
                WHERE org_id = %s AND owner_id = %s AND fingerprint = %s""",
                (job["org_id"], job["owner_id"], fingerprint)).fetchone():
                return None
            existing = self.connection.execute("""SELECT origin, value, updated_at FROM memory_fact
                WHERE org_id = %s AND owner_id = %s AND kind = %s FOR UPDATE""",
                (job["org_id"], job["owner_id"], kind)).fetchone()
            if existing and existing["origin"] == "manual":
                return None
            if existing and job["created_at"] <= existing["updated_at"]:
                return None
            if (kind == "task" and existing and existing["origin"] == "auto"
                    and (topic := _task_topic(existing["value"]))
                    and topic in job["question"] and _task_continues(job["question"])):
                return None
            effect = self.connection.execute("""INSERT INTO memory_job_effect(job_id, kind)
                VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING job_id""",
                (job_id, kind)).fetchone()
            if not effect:
                return None
            row = self.connection.execute("""INSERT INTO memory_fact
                (org_id, owner_id, kind, value, origin, source_conversation_id,
                 created_at, updated_at, expires_at)
                VALUES (%s, %s, %s, %s, 'auto', %s, now(), now(),
                        now() + (%s * interval '1 day'))
                ON CONFLICT (org_id, owner_id, kind) DO UPDATE SET
                    value = excluded.value, origin = 'auto',
                    source_conversation_id = excluded.source_conversation_id,
                    updated_at = excluded.updated_at, expires_at = excluded.expires_at,
                    version = memory_fact.version + 1
                WHERE memory_fact.origin = 'auto'
                RETURNING kind, value, origin, source_conversation_id,
                          created_at, updated_at, expires_at, version""",
                (job["org_id"], job["owner_id"], kind, value,
                 job["source_conversation_id"], FACT_LIFETIMES[kind])).fetchone()
            return self._api_row(row) if row else None

    def upsert_auto(self, subject: Subject, kind: str, value: str,
                    conversation_id: str, source_text: str,
                    *, job_lease: tuple[str, str] | None = None) -> dict | None:
        """Product-shaped entry point, restricted to an existing leased PG job."""
        if (not isinstance(job_lease, tuple) or len(job_lease) != 2
                or not all(isinstance(part, str) for part in job_lease)
                or not isinstance(value, str) or not isinstance(source_text, str)
                or value not in source_text):
            return None
        job_id, lease_token = job_lease
        job = self.connection.execute("""SELECT org_id, owner_id,
            source_conversation_id, question FROM memory_job WHERE id = %s""",
            (job_id,)).fetchone()
        if (job is None or job["org_id"] != subject.org_id
                or job["owner_id"] != subject.user_id
                or job["source_conversation_id"] != conversation_id
                or job["question"] != source_text):
            return None
        return self.apply_auto_fact(job_id, lease_token, kind, value)

    def _product_retraction(self, subject: Subject, kind: str,
                            expected: dict, conversation_id: str,
                            latest_text: str, evidence: str,
                            job_lease: tuple[str, str] | None) -> bool:
        """Bind a product-shaped retraction to its owner and leased job."""
        if (not isinstance(job_lease, tuple) or len(job_lease) != 2
                or not all(isinstance(part, str) for part in job_lease)
                or not isinstance(expected, dict) or expected.get("kind") != kind
                or expected.get("origin") != "auto"
                or expected.get("source_conversation_id") == conversation_id
                or not isinstance(latest_text, str)
                or not isinstance(evidence, str) or evidence not in latest_text):
            return False
        job_id, lease_token = job_lease
        job = self.connection.execute("""SELECT org_id, owner_id,
            source_conversation_id, question FROM memory_job WHERE id = %s""",
            (job_id,)).fetchone()
        if (job is None or job["org_id"] != subject.org_id
                or job["owner_id"] != subject.user_id
                or job["source_conversation_id"] != conversation_id
                or job["question"] != latest_text):
            return False
        return self.retract_auto_fact(job_id, lease_token, expected, evidence)

    def forget_auto_task_cross_session(self, subject: Subject, expected: dict,
                                       conversation_id: str, latest_text: str,
                                       evidence: str,
                                       *, job_lease: tuple[str, str] | None = None) -> bool:
        return self._product_retraction(subject, "task", expected,
                                        conversation_id, latest_text, evidence, job_lease)

    def forget_auto_role_cross_session(self, subject: Subject, expected: dict,
                                       conversation_id: str, latest_text: str,
                                       evidence: str,
                                       *, job_lease: tuple[str, str] | None = None) -> bool:
        return self._product_retraction(subject, "role", expected,
                                        conversation_id, latest_text, evidence, job_lease)

    def forget_auto_from_context(self, subject: Subject, kind: str,
                                 conversation_id: str, context: tuple[str, ...],
                                 *, job_lease: tuple[str, str] | None = None) -> bool:
        """Retract a same-source fact quoted in the leased job's prior user turns."""
        if (kind not in MEMORY_KINDS or not conversation_id
                or not isinstance(context, tuple) or not context
                or not isinstance(job_lease, tuple) or len(job_lease) != 2
                or not all(isinstance(part, str) for part in job_lease)):
            return False
        job_id, token = job_lease
        job = self.connection.execute("""SELECT org_id, owner_id,
            source_conversation_id, question, context_json FROM memory_job WHERE id = %s""",
            (job_id,)).fetchone()
        if (not job or job["org_id"] != subject.org_id
                or job["owner_id"] != subject.user_id
                or job["source_conversation_id"] != conversation_id
                or tuple(job["context_json"]) != context):
            return False
        fact = self.connection.execute("""SELECT kind, value, origin,
            source_conversation_id, version, updated_at FROM memory_fact
            WHERE org_id = %s AND owner_id = %s AND kind = %s
              AND origin = 'auto' AND source_conversation_id = %s""",
            (subject.org_id, subject.user_id, kind, conversation_id)).fetchone()
        if (not fact or not any(isinstance(item, str) and fact["value"] in item
                                for item in context)):
            return False
        evidence = next((term for term in ("不再", "不用", "结束", "移交", "取消", "不要")
                         if term in job["question"]), None)
        if evidence is None:
            return False
        return self.retract_auto_fact(job_id, token, dict(fact), evidence)

    def retract_auto_fact(self, job_id: str, lease_token: str,
                          expected: dict, evidence: str) -> bool:
        """Compare-and-delete one auto fact observed before extraction began."""
        from app.api.memory_suggestions import eligible_auto_memory_text

        if (not isinstance(expected, dict) or expected.get("kind") not in MEMORY_KINDS
                or expected.get("origin") != "auto"
                or not isinstance(expected.get("value"), str)
                or not isinstance(expected.get("source_conversation_id"), str)
                or type(expected.get("version")) is not int
                or not isinstance(expected.get("updated_at"), (datetime, str))
                or not isinstance(evidence, str) or not evidence.strip()
                or len(evidence.strip()) > 160):
            return False
        try:
            observed_at = (datetime.fromisoformat(expected["updated_at"])
                           if isinstance(expected["updated_at"], str)
                           else expected["updated_at"])
        except ValueError:
            return False
        if observed_at.tzinfo is None:
            return False
        kind = expected["kind"]
        with self.connection.transaction():
            identity = self.connection.execute("""SELECT org_id, owner_id,
                source_conversation_id, source_validated FROM memory_job
                WHERE id = %s""", (job_id,)).fetchone()
            if identity is None or not identity["source_validated"]:
                return False
            source = self.connection.execute("""SELECT state FROM memory_source
                WHERE org_id = %s AND owner_id = %s AND id = %s FOR UPDATE""",
                (identity["org_id"], identity["owner_id"],
                 identity["source_conversation_id"])).fetchone()
            if not source or source["state"] != "active":
                return False
            setting = self.connection.execute("""SELECT auto_suggest FROM memory_setting
                WHERE org_id = %s AND owner_id = %s FOR UPDATE""",
                (identity["org_id"], identity["owner_id"])).fetchone()
            if not setting or not setting["auto_suggest"]:
                return False
            job = self.connection.execute("""SELECT *, lease_until > clock_timestamp() AS lease_active
                FROM memory_job WHERE id = %s FOR UPDATE""", (job_id,)).fetchone()
            if (not job or job["state"] != "running" or job["lease_token"] != lease_token
                    or not job["lease_active"] or not eligible_auto_memory_text(job["question"])
                    or evidence.strip() not in job["question"]
                    or "?" in job["question"] or "？" in job["question"]):
                return False
            cutoff = self._lock_guard(job["org_id"], job["owner_id"], kind)
            if job["created_at"] <= cutoff:
                return False
            fact = self.connection.execute("""SELECT kind, value, origin,
                source_conversation_id, version, updated_at, expires_at FROM memory_fact
                WHERE org_id = %s AND owner_id = %s AND kind = %s FOR UPDATE""",
                (job["org_id"], job["owner_id"], kind)).fetchone()
            if (not fact or fact["origin"] != "auto"
                    or fact["value"] != expected["value"]
                    or fact["version"] != expected["version"]
                    or fact["updated_at"] != observed_at
                    or fact["source_conversation_id"] != expected["source_conversation_id"]
                    or (fact["expires_at"] is not None
                        and fact["expires_at"] <= datetime.now(timezone.utc))):
                return False
            if fact["source_conversation_id"] == job["source_conversation_id"]:
                context = job["context_json"]
                if (not isinstance(context, list)
                        or not any(isinstance(item, str) and fact["value"] in item
                                   for item in context)
                        or not any(term in job["question"] for term in
                                   ("不再", "不用", "结束", "移交", "取消", "不要"))):
                    return False
            elif kind == "task":
                topic = _task_topic(fact["value"])
                if (len(topic) < 6 or topic not in job["question"]
                        or not any(term in evidence for term in
                                   ("已移交", "已经移交", "不再负责", "不再跟进",
                                    "已经结束", "已结束", "结束了", "已经完成", "已完成", "完成了"))
                        or _task_continues(job["question"])
                        or (re.search(r"(?:一期|二期|阶段)", job["question"])
                            and not re.search(r"(?:不再负责|不再跟进|已移交|已经移交)",
                                              job["question"]))):
                    return False
            elif kind == "role":
                role = fact["value"].removeprefix("我是").strip()
                if (len(role) < 2 or role not in job["question"]
                        or not re.search(r"我.{0,4}(?:不再做|不再是|不做|已不做|"
                                         r"已经不做|已经不是|离开)", evidence)):
                    return False
            else:
                # Cross-session answer-preference retractions need a separate gate.
                return False
            effect = self.connection.execute("""INSERT INTO memory_job_effect(job_id, kind)
                VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING job_id""",
                (job_id, kind)).fetchone()
            if not effect:
                return False
            self._guard_fact(job["org_id"], job["owner_id"], kind)
            fingerprint = hashlib.sha256(
                f"{kind}\0{fact['value'].strip().casefold()}".encode()).hexdigest()
            self.connection.execute("""INSERT INTO memory_suppression
                (org_id, owner_id, fingerprint) VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING""",
                (job["org_id"], job["owner_id"], fingerprint))
            deleted = self.connection.execute("""DELETE FROM memory_fact
                WHERE org_id = %s AND owner_id = %s AND kind = %s AND version = %s
                AND origin = 'auto' RETURNING kind""",
                (job["org_id"], job["owner_id"], kind, fact["version"])).fetchone()
            return deleted is not None

    def finish_suggestion_job(self, job_id: str, lease_token: str) -> bool:
        with self.connection.transaction():
            result = self.connection.execute("""DELETE FROM memory_job WHERE id = %s
                AND state = 'running' AND lease_token = %s AND lease_until > clock_timestamp()""",
                (job_id, lease_token))
        return result.rowcount == 1

    def fail_suggestion_job(self, job_id: str, lease_token: str,
                            reason_code: str, *, retryable: bool) -> bool:
        if reason_code not in FAILURE_CODES:
            raise ValueError("INVALID_MEMORY_FAILURE_CODE")
        with self.connection.transaction():
            job = self.connection.execute("""SELECT *, lease_until > clock_timestamp() AS lease_active
                FROM memory_job WHERE id = %s
                FOR UPDATE""", (job_id,)).fetchone()
            if (not job or job["state"] != "running" or job["lease_token"] != lease_token
                    or not job["lease_active"]):
                return False
            if retryable and job["attempts"] < MAX_JOB_ATTEMPTS:
                self.connection.execute("""UPDATE memory_job SET state = 'queued',
                    lease_token = NULL, lease_until = NULL,
                    available_at = now() + (%s * interval '1 second') WHERE id = %s""",
                    (2 ** job["attempts"], job_id))
            else:
                self.connection.execute("""INSERT INTO memory_job_failure
                    (job_id, org_id, owner_id, source_conversation_id, reason_code,
                     attempts, failed_at) VALUES (%s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (job_id) DO NOTHING""",
                    (job_id, job["org_id"], job["owner_id"],
                     job["source_conversation_id"], reason_code, job["attempts"]))
                self.connection.execute("DELETE FROM memory_job WHERE id = %s", (job_id,))
            return True

    def list_suggestion_failures(self, subject: Subject) -> list[dict]:
        return [self._api_row(row) for row in self.connection.execute("""SELECT job_id, reason_code,
            attempts, failed_at FROM memory_job_failure WHERE org_id = %s AND owner_id = %s
            ORDER BY failed_at DESC LIMIT 30""", (subject.org_id, subject.user_id))]

    def suggestion_job_count(self, subject: Subject | None = None) -> int:
        if subject is None:
            return self.connection.execute("SELECT count(*) AS n FROM memory_job").fetchone()["n"]
        return self.connection.execute("""SELECT count(*) AS n FROM memory_job
            WHERE org_id = %s AND owner_id = %s""",
            (subject.org_id, subject.user_id)).fetchone()["n"]

    def close(self) -> None:
        self.connection.close()
