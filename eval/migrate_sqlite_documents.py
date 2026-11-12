"""Verify or import a stopped SQLite document catalog into a lab PostgreSQL schema.

This tool never switches the product API. Apply is restricted to agent_memory_lab
and an isolated document_test_* schema.
"""

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from eval.migrate_sqlite_conversations import file_sha256


TABLES = {
    "documents": ("document_id", "org_id", "title", "file_type", "area", "version",
                  "updated_at", "owner", "location", "excerpt", "state",
                  "classification", "visibility", "department_ids", "allowed_user_ids",
                  "denied_user_ids"),
    "document_files": ("document_id", "filename", "content", "extracted_text"),
    "document_revisions": ("document_id", "version", "submitted_by", "filename",
                           "file_type", "content", "extracted_text", "status", "created_at"),
    "document_segments": ("segment_id", "document_id", "location", "text"),
    "document_segment_terms": ("segment_id", "term"),
    "search_index_meta": ("version",),
    "search_policy_epoch": ("version",),
    "search_index_source": ("source_id",),
    "search_index_outbox": ("event_id", "document_id", "created_at", "attempts",
                            "processed_at", "last_error"),
}
OPTIONAL_TABLES = {"search_index_source", "search_index_outbox"}

SCHEMA_SQL = """
CREATE TABLE documents (
  document_id text PRIMARY KEY, org_id text NOT NULL, title text NOT NULL,
  file_type text NOT NULL, area text NOT NULL, version text NOT NULL,
  updated_at text NOT NULL, owner text NOT NULL, location text NOT NULL,
  excerpt text NOT NULL, state text NOT NULL, classification text NOT NULL,
  visibility text NOT NULL, department_ids text NOT NULL,
  allowed_user_ids text NOT NULL, denied_user_ids text NOT NULL
);
CREATE TABLE document_files (
  document_id text PRIMARY KEY, filename text NOT NULL,
  content bytea NOT NULL, extracted_text text NOT NULL
);
CREATE TABLE document_revisions (
  document_id text NOT NULL, version text NOT NULL, submitted_by text NOT NULL,
  filename text NOT NULL, file_type text NOT NULL, content bytea NOT NULL,
  extracted_text text NOT NULL, status text NOT NULL, created_at text NOT NULL,
  PRIMARY KEY (document_id, version)
);
CREATE TABLE document_segments (
  segment_id bigint PRIMARY KEY, document_id text NOT NULL,
  location text NOT NULL, text text NOT NULL
);
CREATE TABLE document_segment_terms (
  segment_id bigint NOT NULL, term text NOT NULL, PRIMARY KEY (segment_id, term)
);
CREATE TABLE search_index_meta (version integer NOT NULL);
CREATE TABLE search_policy_epoch (version integer NOT NULL);
CREATE TABLE search_index_source (source_id text NOT NULL);
CREATE TABLE search_index_outbox (
  event_id bigint PRIMARY KEY, document_id text NOT NULL, created_at text NOT NULL,
  attempts integer NOT NULL, processed_at text, last_error text
);
"""


def _canonical(row: dict) -> dict:
    return {name: {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
            if isinstance(value, (bytes, memoryview)) else value
            for name, value in row.items()}


def _snapshot(connection, *, sqlite_source: bool) -> tuple[dict, dict]:
    available = ({row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if sqlite_source else None)
    payload, summary = {}, {}
    for table, columns in TABLES.items():
        if available is not None and table not in available:
            if table not in OPTIONAL_TABLES:
                raise ValueError("DOCUMENT_SNAPSHOT_SCHEMA_MISSING")
            rows = []
        else:
            if sqlite_source:
                query = f"SELECT {', '.join(columns)} FROM {table}"
            else:
                query = sql.SQL("SELECT {} FROM {}").format(
                    sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table))
            rows = [dict(row) for row in connection.execute(query).fetchall()]
        payload[table] = rows
        canonical = [_canonical(row) for row in rows]
        canonical.sort(key=lambda row: json.dumps(row, ensure_ascii=False,
                                                  sort_keys=True, separators=(",", ":")))
        digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        summary[table] = {"count": len(rows), "sha256": digest}
    return payload, summary


def source_snapshot(path: Path) -> tuple[dict, dict, str]:
    path = path.resolve(strict=True)
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("DOCUMENT_SNAPSHOT_HAS_ACTIVE_JOURNAL")
    before = file_sha256(path)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("DOCUMENT_SNAPSHOT_INTEGRITY_FAILED")
        payload, summary = _snapshot(connection, sqlite_source=True)
    finally:
        connection.close()
    if file_sha256(path) != before:
        raise RuntimeError("DOCUMENT_SNAPSHOT_CHANGED_DURING_READ")
    return payload, summary, before


def apply_snapshot(path: Path, dsn: str, *, schema: str, expected_sha256: str) -> dict:
    if conninfo_to_dict(dsn).get("dbname") != "agent_memory_lab":
        raise ValueError("DOCUMENT_LAB_DATABASE_REQUIRED")
    if not schema.startswith("document_test_") or len(schema) < 25:
        raise ValueError("ISOLATED_DOCUMENT_SCHEMA_REQUIRED")
    payload, source_summary, source_hash = source_snapshot(path)
    if source_hash != expected_sha256:
        raise ValueError("DOCUMENT_SNAPSHOT_HASH_MISMATCH")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        with connection.transaction():
            exists = connection.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                (schema,)).fetchone()
            if exists:
                raise ValueError("DOCUMENT_TARGET_SCHEMA_EXISTS")
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(
                sql.Identifier(schema)))
            connection.execute(SCHEMA_SQL)
            for table, columns in TABLES.items():
                query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns))
                for row in payload[table]:
                    connection.execute(query, tuple(row[name] for name in columns))
            _, target_summary = _snapshot(connection, sqlite_source=False)
            if target_summary != source_summary:
                raise RuntimeError("DOCUMENT_IMPORT_MISMATCH")
    return {"source_sha256": source_hash, "schema": schema,
            "tables": source_summary, "verified": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--schema")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.apply:
        if not args.expected_sha256 or not args.schema:
            raise ValueError("EXPECTED_HASH_AND_SCHEMA_REQUIRED")
        dsn = os.getenv("MEMORY_TEST_DATABASE_URL", "")
        if not dsn:
            raise ValueError("MEMORY_TEST_DATABASE_URL_REQUIRED")
        result = apply_snapshot(args.snapshot, dsn, schema=args.schema,
                                expected_sha256=args.expected_sha256)
    else:
        _, summary, source_hash = source_snapshot(args.snapshot)
        result = {"source_sha256": source_hash, "tables": summary, "applied": False}
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
