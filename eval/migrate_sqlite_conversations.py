"""Dry-run or import a stopped SQLite conversation snapshot into a lab schema.

The product API is not switched by this script. The source file is read-only,
and apply is limited to the dedicated agent_memory_lab database.
"""

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.api.pg_conversations import PgConversationRepository


TABLES = {
    "conversations": ("id", "org_id", "owner_id", "title", "updated_at"),
    "conversation_turns": ("id", "conversation_id", "question", "mode",
                           "answer_json", "workflow_run_id"),
    "conversation_delete_outbox": (
        "id", "org_id", "owner_id", "conversation_id", "created_at", "available_at",
        "delivered_at", "lease_token", "lease_until", "attempts", "last_error_code"),
    "conversation_memory_outbox": (
        "id", "org_id", "owner_id", "conversation_id", "turn_id",
        "include_ordinary", "created_at", "available_at", "delivered_at",
        "lease_token", "lease_until", "attempts", "last_error_code"),
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def _rows(connection, table: str, columns: tuple[str, ...]) -> list[dict]:
    names = ", ".join(columns)
    query = f"SELECT {names} FROM {table} ORDER BY id"
    records = connection.execute(query).fetchall()
    return [dict(record) for record in records]


def _snapshot(connection, *, legacy_sqlite: bool = False) -> tuple[dict, dict]:
    payload, summary = {}, {}
    available = ({row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
        if legacy_sqlite else None)
    for table, columns in TABLES.items():
        if available is not None and table not in available:
            if table not in ("conversation_delete_outbox", "conversation_memory_outbox"):
                raise ValueError("CONVERSATION_SNAPSHOT_SCHEMA_MISSING")
            records = []
        else:
            records = _rows(connection, table, columns)
        payload[table] = records
        summary[table] = {"count": len(records), "sha256": _digest(records)}
    return payload, summary


def source_snapshot(path: Path) -> tuple[dict, dict, str]:
    path = path.resolve(strict=True)
    for suffix in ("-wal", "-shm"):
        if Path(str(path) + suffix).exists():
            raise ValueError("CONVERSATION_SNAPSHOT_HAS_WAL_OR_SHM")
    before = file_sha256(path)
    database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    try:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("CONVERSATION_SNAPSHOT_INTEGRITY_FAILED")
        payload, summary = _snapshot(database, legacy_sqlite=True)
    finally:
        database.close()
    if file_sha256(path) != before:
        raise RuntimeError("CONVERSATION_SNAPSHOT_CHANGED_DURING_READ")
    return payload, summary, before


def apply_snapshot(path: Path, dsn: str, *, schema: str,
                   expected_sha256: str, target_database: str = "agent_memory_lab") -> dict:
    if target_database not in ("agent_memory_lab", "agent_memory"):
        raise ValueError("INVALID_CONVERSATION_TARGET_DATABASE")
    if conninfo_to_dict(dsn).get("dbname") != target_database:
        raise ValueError("CONVERSATION_TARGET_DATABASE_MISMATCH")
    if target_database == "agent_memory_lab":
        if not schema.startswith("conversation_test_") or len(schema) < 25:
            raise ValueError("ISOLATED_CONVERSATION_SCHEMA_REQUIRED")
    elif schema != "memory":
        raise ValueError("PRODUCT_CONVERSATION_SCHEMA_REQUIRED")
    payload, source_summary, source_hash = source_snapshot(path)
    if source_hash != expected_sha256:
        raise ValueError("CONVERSATION_SNAPSHOT_HASH_MISMATCH")
    repository = PgConversationRepository(dsn, schema=schema, create_schema=True)
    try:
        connection = repository.connection
        with repository._lock, connection.transaction():
            _, before = _snapshot(connection)
            if any(row["count"] for row in before.values()):
                raise ValueError("CONVERSATION_TARGET_NOT_EMPTY")
            for table, columns in TABLES.items():
                query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns))
                for row in payload[table]:
                    connection.execute(query, tuple(row[name] for name in columns))
            maximum = connection.execute(
                "SELECT COALESCE(max(id), 0) AS id FROM conversation_turns").fetchone()["id"]
            connection.execute(
                "SELECT setval(pg_get_serial_sequence('conversation_turns', 'id'), %s, %s)",
                (maximum if maximum else 1, bool(maximum)))
            _, target_summary = _snapshot(connection)
            if source_summary != target_summary:
                raise RuntimeError("CONVERSATION_IMPORT_MISMATCH")
        return {"source_sha256": source_hash, "schema": schema,
                "tables": source_summary, "verified": True}
    finally:
        repository.close()


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
