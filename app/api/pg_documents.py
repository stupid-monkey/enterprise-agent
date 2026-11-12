"""Isolated PostgreSQL document authority experiment; not wired into the product API."""

import json
import re

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from app.api.documents import DocumentRecord, DocumentRepository, OriginalVersionChanged
from app.security.policy import ResourceAction, Subject, authorize


class PgDocumentRepository:
    """Read imported catalog data and commit ACL changes with epoch and index events."""

    def __init__(self, dsn: str, *, schema: str):
        if conninfo_to_dict(dsn).get("dbname") != "agent_memory_lab":
            raise ValueError("DOCUMENT_LAB_DATABASE_REQUIRED")
        if not re.fullmatch(r"document_test_[a-z0-9]{12,48}", schema):
            raise ValueError("ISOLATED_DOCUMENT_SCHEMA_REQUIRED")
        self.connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self.schema = schema
        try:
            with self.connection.transaction():
                self.connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(
                    sql.Identifier(schema)))
                self.connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (schema,))
                if self.connection.execute("SELECT to_regclass('documents') AS name").fetchone()["name"] is None:
                    raise ValueError("DOCUMENT_IMPORTED_SCHEMA_REQUIRED")
                self.connection.execute("""CREATE TABLE IF NOT EXISTS document_store_version (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                    version integer NOT NULL)""")
                self.connection.execute("CREATE SEQUENCE IF NOT EXISTS document_index_event_seq")
                inserted = self.connection.execute("""INSERT INTO document_store_version
                    (singleton, version) VALUES (true, 1)
                    ON CONFLICT (singleton) DO NOTHING RETURNING version""").fetchone()
                if inserted is not None:
                    last_event = self.connection.execute(
                        "SELECT COALESCE(MAX(event_id), 0) AS id FROM search_index_outbox").fetchone()["id"]
                    if last_event:
                        self.connection.execute("SELECT setval('document_index_event_seq', %s, true)",
                                                (last_event,))
                version = self.connection.execute(
                    "SELECT version FROM document_store_version WHERE singleton = true").fetchone()
                if version is None or version["version"] != 1:
                    raise ValueError("DOCUMENT_STORE_VERSION_UNSUPPORTED")
            self.connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _record(row: dict) -> DocumentRecord:
        return DocumentRepository._record(row)

    def policy_epoch(self) -> int:
        row = self.connection.execute("SELECT version FROM search_policy_epoch LIMIT 1").fetchone()
        if row is None:
            raise ValueError("DOCUMENT_POLICY_EPOCH_MISSING")
        return row["version"]

    def list_visible(self, subject: Subject) -> list[DocumentRecord]:
        rows = self.connection.execute("""SELECT d.*, f.document_id AS file_document_id
            FROM documents d LEFT JOIN document_files f USING(document_id)
            WHERE d.org_id = %s ORDER BY d.document_id""", (subject.org_id,)).fetchall()
        records = (self._record(row) for row in rows)
        return [record for record in records
                if authorize(subject, record.policy, ResourceAction.READ).allowed]

    def get_visible(self, subject: Subject, document_id: str) -> DocumentRecord | None:
        row = self.connection.execute("""SELECT d.*, f.document_id AS file_document_id
            FROM documents d LEFT JOIN document_files f USING(document_id)
            WHERE d.org_id = %s AND d.document_id = %s""",
            (subject.org_id, document_id)).fetchone()
        if row is None:
            return None
        record = self._record(row)
        return record if authorize(subject, record.policy, ResourceAction.READ).allowed else None

    def text_for(self, subject: Subject, document_id: str,
                 action: ResourceAction) -> tuple[DocumentRecord, str] | None:
        row = self.connection.execute("""SELECT d.*, f.document_id AS file_document_id,
            f.extracted_text FROM documents d LEFT JOIN document_files f USING(document_id)
            WHERE d.org_id = %s AND d.document_id = %s""",
            (subject.org_id, document_id)).fetchone()
        if row is None:
            return None
        record = self._record(row)
        if not authorize(subject, record.policy, action).allowed:
            return None
        return record, row["extracted_text"] or record.excerpt

    def original_for(self, subject: Subject, document_id: str,
                     expected_version: str | None = None) -> tuple[str, str, bytes] | None:
        row = self.connection.execute("""SELECT d.*, f.document_id AS file_document_id,
            f.filename, f.content FROM documents d LEFT JOIN document_files f USING(document_id)
            WHERE d.org_id = %s AND d.document_id = %s""",
            (subject.org_id, document_id)).fetchone()
        if row is None:
            return None
        record = self._record(row)
        if not authorize(subject, record.policy, ResourceAction.DOWNLOAD).allowed:
            return None
        if expected_version is not None and record.version != expected_version:
            raise OriginalVersionChanged
        if row["content"] is None:
            return None
        return row["filename"], record.file_type, bytes(row["content"])

    def set_denied(self, org_id: str, document_id: str, user_id: str, denied: bool) -> bool:
        with self.connection.transaction():
            row = self.connection.execute("""SELECT denied_user_ids FROM documents
                WHERE org_id = %s AND document_id = %s FOR UPDATE""",
                (org_id, document_id)).fetchone()
            if row is None:
                return False
            users = set(json.loads(row["denied_user_ids"]))
            if denied:
                users.add(user_id)
            else:
                users.discard(user_id)
            self.connection.execute("""UPDATE documents SET denied_user_ids = %s
                WHERE org_id = %s AND document_id = %s""",
                (json.dumps(sorted(users)), org_id, document_id))
            updated = self.connection.execute("""UPDATE search_policy_epoch
                SET version = version + 1 RETURNING version""").fetchone()
            if updated is None:
                raise ValueError("DOCUMENT_POLICY_EPOCH_MISSING")
            self.connection.execute("""INSERT INTO search_index_outbox
                (event_id, document_id, created_at, attempts, processed_at, last_error)
                VALUES (nextval('document_index_event_seq'), %s,
                        (now() AT TIME ZONE 'UTC')::text, 0, NULL, NULL)""", (document_id,))
        return True
