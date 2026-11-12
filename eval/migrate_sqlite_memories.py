"""Dry-run or import an offline SQLite memory snapshot into an empty PG memory DB."""

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.api.pg_user_memories import PgUserMemoryRepository


def read_snapshot(path: Path, *, as_of: datetime | None = None) -> dict:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.with_name(path.name + "-wal").exists():
        raise ValueError("SQLITE_SNAPSHOT_HAS_WAL")
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"user_memories", "memory_settings", "memory_suppression"}
        if not required <= names:
            raise ValueError("SQLITE_MEMORY_SCHEMA_MISSING")
        now = as_of or datetime.now(timezone.utc)
        facts = []
        for row in db.execute("""SELECT org_id, owner_id, kind, value, origin,
                source_conversation_id, created_at, updated_at, expires_at FROM user_memories"""):
            item = dict(row)
            expires = item["expires_at"]
            if expires and datetime.fromisoformat(expires) <= now:
                continue
            if item["origin"] == "auto" and not item["source_conversation_id"]:
                raise ValueError("AUTO_FACT_WITHOUT_SOURCE")
            facts.append(item)
        settings = [dict(row) for row in db.execute(
            "SELECT org_id, owner_id, auto_suggest FROM memory_settings")]
        suppressions = [dict(row) for row in db.execute(
            "SELECT org_id, owner_id, fingerprint FROM memory_suppression")]
        suggestions = ([dict(row) for row in db.execute("""SELECT id, org_id, owner_id,
            kind, value, source_conversation_id, source_text, created_at
            FROM memory_suggestions""")] if "memory_suggestions" in names else [])
        pending_jobs = (db.execute("SELECT count(*) FROM memory_suggestion_jobs").fetchone()[0]
                        if "memory_suggestion_jobs" in names else 0)
    finally:
        db.close()
    if hashlib.sha256(path.read_bytes()).hexdigest() != source_hash:
        raise ValueError("SQLITE_SNAPSHOT_CHANGED_DURING_READ")
    facts.sort(key=lambda item: (item["org_id"], item["owner_id"], item["kind"]))
    settings.sort(key=lambda item: (item["org_id"], item["owner_id"]))
    suppressions.sort(key=lambda item: (item["org_id"], item["owner_id"], item["fingerprint"]))
    suggestions.sort(key=lambda item: (item["org_id"], item["owner_id"], item["id"]))
    return {"source_sha256": source_hash, "facts": facts, "settings": settings,
            "suppressions": suppressions, "suggestions": suggestions,
            "pending_jobs": pending_jobs}


def read_conversation_snapshot(path: Path) -> dict:
    """Read owner-scoped source IDs from a stopped SQLite conversation copy."""
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.with_name(path.name + "-wal").exists():
        raise ValueError("SQLITE_SNAPSHOT_HAS_WAL")
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        sources = [dict(zip(("org_id", "owner_id", "id"), row)) for row in
                   db.execute("SELECT org_id, owner_id, id FROM conversations")]
    finally:
        db.close()
    if hashlib.sha256(path.read_bytes()).hexdigest() != source_hash:
        raise ValueError("SQLITE_SNAPSHOT_CHANGED_DURING_READ")
    sources.sort(key=lambda item: (item["org_id"], item["owner_id"], item["id"]))
    return {"source_sha256": source_hash, "sources": sources}


def digest(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def import_snapshot(snapshot: dict, dsn: str, *, schema: str = "memory",
                    target_database: str = "agent_memory_lab",
                    conversations: dict | None = None) -> dict:
    if target_database not in ("agent_memory_lab", "agent_memory"):
        raise ValueError("INVALID_MEMORY_TARGET_DATABASE")
    if target_database == "agent_memory":
        if conversations is None:
            raise ValueError("CONVERSATION_SNAPSHOT_REQUIRED")
        if snapshot["pending_jobs"]:
            raise RuntimeError("PENDING_MEMORY_JOBS_MUST_DRAIN")
        live = {(row["org_id"], row["owner_id"], row["id"])
                for row in conversations["sources"]}
        referenced = {(row["org_id"], row["owner_id"], row["source_conversation_id"])
                      for row in snapshot["facts"] if row["origin"] == "auto"}
        referenced.update((row["org_id"], row["owner_id"], row["source_conversation_id"])
                          for row in snapshot["suggestions"])
        if not referenced <= live:
            raise RuntimeError("MEMORY_SOURCE_NOT_IN_CONVERSATION_SNAPSHOT")
    repository = PgUserMemoryRepository(dsn, schema=schema)
    try:
        database_name = repository.connection.execute(
            "SELECT current_database() AS name").fetchone()["name"]
        if database_name != target_database:
            raise RuntimeError("MEMORY_MIGRATION_LAB_DATABASE_REQUIRED" if target_database ==
                               "agent_memory_lab" else "MEMORY_MIGRATION_TARGET_DATABASE_MISMATCH")
        repository.create_schema()
        connection = repository.connection
        with connection.transaction():
            # This first slice only imports into a fresh destination. Never overwrite live data.
            counts = [connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                      for table in ("memory_fact", "memory_setting", "memory_suppression",
                                    "memory_suggestion", "memory_source", "memory_job")]
            if any(counts):
                raise RuntimeError("POSTGRES_MEMORY_DESTINATION_NOT_EMPTY")
            for row in snapshot["facts"]:
                connection.execute("""INSERT INTO memory_fact
                    (org_id, owner_id, kind, value, origin, source_conversation_id,
                     created_at, updated_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    tuple(row[field] for field in ("org_id", "owner_id", "kind", "value",
                        "origin", "source_conversation_id", "created_at", "updated_at", "expires_at")))
            for row in snapshot["settings"]:
                connection.execute("""INSERT INTO memory_setting
                    (org_id, owner_id, auto_suggest) VALUES (%s, %s, %s)""",
                    (row["org_id"], row["owner_id"], bool(row["auto_suggest"])))
            for row in snapshot["suppressions"]:
                connection.execute("""INSERT INTO memory_suppression
                    (org_id, owner_id, fingerprint) VALUES (%s, %s, %s)""",
                    (row["org_id"], row["owner_id"], row["fingerprint"]))
            for row in snapshot["suggestions"]:
                connection.execute("""INSERT INTO memory_suggestion
                    (id, org_id, owner_id, kind, value, source_conversation_id,
                     source_text, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    tuple(row[field] for field in ("id", "org_id", "owner_id", "kind", "value",
                                                  "source_conversation_id", "source_text", "created_at")))
            if conversations is not None:
                for row in conversations["sources"]:
                    connection.execute("""INSERT INTO memory_source
                        (org_id, owner_id, id, state, created_at)
                        VALUES (%s, %s, %s, 'active', now())""",
                        (row["org_id"], row["owner_id"], row["id"]))
            copied = {"facts": [dict(row) for row in connection.execute("""SELECT
                    org_id, owner_id, kind, value, origin, source_conversation_id,
                    created_at, updated_at, expires_at FROM memory_fact
                    ORDER BY org_id, owner_id, kind""")],
                "settings": [dict(row) for row in connection.execute("""SELECT org_id,
                    owner_id, auto_suggest FROM memory_setting ORDER BY org_id, owner_id""")],
                "suppressions": [dict(row) for row in connection.execute("""SELECT org_id,
                    owner_id, fingerprint FROM memory_suppression
                    ORDER BY org_id, owner_id, fingerprint""")],
                "suggestions": [dict(row) for row in connection.execute("""SELECT id,
                    org_id, owner_id, kind, value, source_conversation_id,
                    source_text, created_at FROM memory_suggestion
                    ORDER BY org_id, owner_id, id""")]}
            for row in copied["facts"]:
                for field in ("created_at", "updated_at", "expires_at"):
                    if row[field] is not None:
                        row[field] = row[field].isoformat(timespec="microseconds")
            for row in copied["settings"]:
                row["auto_suggest"] = int(row["auto_suggest"])
            for row in copied["suggestions"]:
                row["created_at"] = row["created_at"].isoformat(timespec="microseconds")
            for key in copied:
                if digest(copied[key]) != digest(snapshot[key]):
                    raise RuntimeError(f"POSTGRES_MEMORY_VERIFY_FAILED:{key}")
            if conversations is not None:
                copied_sources = [dict(row) for row in connection.execute("""SELECT
                    org_id, owner_id, id FROM memory_source WHERE state = 'active'
                    ORDER BY org_id, owner_id, id""")]
                if digest(copied_sources) != digest(conversations["sources"]):
                    raise RuntimeError("POSTGRES_MEMORY_VERIFY_FAILED:sources")
            if target_database == "agent_memory":
                connection.execute("""CREATE TABLE IF NOT EXISTS memory_import_manifest (
                    id integer PRIMARY KEY CHECK (id = 1),
                    memory_sha256 text NOT NULL, conversation_sha256 text NOT NULL,
                    imported_at timestamptz NOT NULL
                )""")
                connection.execute("""INSERT INTO memory_import_manifest
                    (id, memory_sha256, conversation_sha256, imported_at)
                    VALUES (1, %s, %s, now())""",
                    (snapshot["source_sha256"], conversations["source_sha256"]))
        return {"verified": True}
    finally:
        repository.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True,
                        help="Offline SQLite copy; stop writers before making this snapshot")
    parser.add_argument("--apply", action="store_true", help="Import into empty PG memory DB")
    parser.add_argument("--expected-sha256", help="Required for apply; copy hash from dry run")
    parser.add_argument("--schema", default="memory", help="Destination PostgreSQL schema")
    parser.add_argument("--target-database", choices=("agent_memory_lab", "agent_memory"),
                        default="agent_memory_lab")
    parser.add_argument("--conversation-snapshot", type=Path,
                        help="Required for agent_memory; stopped SQLite conversation copy")
    parser.add_argument("--expected-conversation-sha256",
                        help="Required for agent_memory apply; recorded conversation snapshot hash")
    parser.add_argument("--output", type=Path, help="Write metadata-only report")
    args = parser.parse_args()
    snapshot = read_snapshot(args.snapshot)
    conversations = (read_conversation_snapshot(args.conversation_snapshot)
                     if args.conversation_snapshot else None)
    report = {"source_sha256": snapshot["source_sha256"],
              "counts": {key: len(snapshot[key]) for key in ("facts", "settings", "suppressions", "suggestions")},
              "digests": {key: digest(snapshot[key]) for key in ("facts", "settings", "suppressions", "suggestions")},
              "pending_jobs": snapshot["pending_jobs"],
              "mode": "apply" if args.apply else "dry_run"}
    if conversations is not None:
        report["conversation_source_sha256"] = conversations["source_sha256"]
        report["conversation_source_count"] = len(conversations["sources"])
    if args.apply:
        if not args.expected_sha256 or args.expected_sha256 != snapshot["source_sha256"]:
            raise ValueError("MEMORY_SNAPSHOT_HASH_MISMATCH")
        if args.target_database == "agent_memory" and (conversations is None or
                args.expected_conversation_sha256 != conversations["source_sha256"]):
            raise ValueError("CONVERSATION_SNAPSHOT_HASH_MISMATCH")
        dsn = os.getenv("MEMORY_DATABASE_URL", "")
        if not dsn:
            raise ValueError("MEMORY_DATABASE_URL_REQUIRED")
        report.update(import_snapshot(snapshot, dsn, schema=args.schema,
                                      target_database=args.target_database,
                                      conversations=conversations))
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
