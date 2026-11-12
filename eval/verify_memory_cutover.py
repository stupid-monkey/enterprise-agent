"""Read-only comparison of an offline SQLite memory snapshot and a PG lab schema."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.api.pg_user_memories import PgUserMemoryRepository, SCHEMA_VERSION
from eval.migrate_sqlite_memories import (digest, read_conversation_snapshot,
                                          read_snapshot)


def verify_snapshot(snapshot_path: Path, expected_sha256: str, dsn: str,
                    *, schema: str = "memory",
                    target_database: str = "agent_memory_lab",
                    conversation_snapshot_path: Path | None = None,
                    expected_conversation_sha256: str | None = None) -> dict:
    """Compare current facts/settings/suppression without writing either store."""
    if not expected_sha256:
        raise ValueError("MEMORY_SNAPSHOT_HASH_REQUIRED")
    if not dsn:
        raise ValueError("MEMORY_DATABASE_URL_REQUIRED")
    as_of = datetime.now(timezone.utc)
    snapshot = read_snapshot(snapshot_path, as_of=as_of)
    if snapshot["source_sha256"] != expected_sha256:
        raise ValueError("MEMORY_SNAPSHOT_HASH_MISMATCH")
    conversations = (read_conversation_snapshot(conversation_snapshot_path)
                     if conversation_snapshot_path else None)
    if target_database == "agent_memory" and (conversations is None or
            conversations["source_sha256"] != expected_conversation_sha256):
        raise ValueError("CONVERSATION_SNAPSHOT_HASH_MISMATCH")
    repository = PgUserMemoryRepository(dsn, schema=schema)
    try:
        connection = repository.connection
        if connection.execute("SELECT current_database() AS name").fetchone()["name"] != target_database:
            raise RuntimeError("MEMORY_VERIFY_LAB_DATABASE_REQUIRED" if target_database ==
                               "agent_memory_lab" else "MEMORY_VERIFY_TARGET_DATABASE_MISMATCH")
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            version = connection.execute(
                "SELECT version FROM memory_schema_version").fetchall()
            if [row["version"] for row in version] != [SCHEMA_VERSION]:
                raise RuntimeError("UNSUPPORTED_MEMORY_SCHEMA_VERSION")
            if target_database == "agent_memory":
                manifest = connection.execute("""SELECT memory_sha256,
                    conversation_sha256 FROM memory_import_manifest WHERE id = 1""").fetchone()
                if (manifest is None or manifest["memory_sha256"] != expected_sha256
                        or manifest["conversation_sha256"] != expected_conversation_sha256):
                    raise RuntimeError("MEMORY_IMPORT_MANIFEST_MISMATCH")
            target = {
                "facts": [dict(row) for row in connection.execute("""SELECT
                    org_id, owner_id, kind, value, origin, source_conversation_id,
                    created_at, updated_at, expires_at FROM memory_fact
                    WHERE expires_at IS NULL OR expires_at > %s
                    ORDER BY org_id, owner_id, kind""", (as_of,))],
                "settings": [dict(row) for row in connection.execute("""SELECT
                    org_id, owner_id, auto_suggest FROM memory_setting
                    ORDER BY org_id, owner_id""")],
                "suppressions": [dict(row) for row in connection.execute("""SELECT
                    org_id, owner_id, fingerprint FROM memory_suppression
                    ORDER BY org_id, owner_id, fingerprint""")],
                "suggestions": [dict(row) for row in connection.execute("""SELECT id,
                    org_id, owner_id, kind, value, source_conversation_id,
                    source_text, created_at FROM memory_suggestion
                    ORDER BY org_id, owner_id, id""")],
            }
            if conversations is not None:
                target["sources"] = [dict(row) for row in connection.execute("""SELECT
                    org_id, owner_id, id FROM memory_source WHERE state = 'active'
                    ORDER BY org_id, owner_id, id""")]
        for row in target["facts"]:
            for field in ("created_at", "updated_at", "expires_at"):
                if row[field] is not None:
                    row[field] = row[field].isoformat(timespec="microseconds")
        for row in target["settings"]:
            row["auto_suggest"] = int(row["auto_suggest"])
        for row in target["suggestions"]:
            row["created_at"] = row["created_at"].isoformat(timespec="microseconds")
        expected = {key: snapshot[key] for key in
                    ("facts", "settings", "suppressions", "suggestions")}
        if conversations is not None:
            expected["sources"] = conversations["sources"]
        checks = {key: {"source_count": len(rows), "target_count": len(target[key]),
                        "match": digest(rows) == digest(target[key])}
                  for key, rows in expected.items()}
        return {"source_sha256": expected_sha256, "checks": checks,
                "verified": all(check["match"] for check in checks.values())}
    finally:
        repository.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True,
                        help="Offline SQLite copy made after stopping memory writes")
    parser.add_argument("--expected-sha256", required=True,
                        help="Hash recorded when the snapshot was created")
    parser.add_argument("--schema", default="memory")
    parser.add_argument("--target-database", choices=("agent_memory_lab", "agent_memory"),
                        default="agent_memory_lab")
    parser.add_argument("--conversation-snapshot", type=Path)
    parser.add_argument("--expected-conversation-sha256")
    args = parser.parse_args()
    result = verify_snapshot(args.snapshot, args.expected_sha256,
                             os.getenv("MEMORY_DATABASE_URL", ""), schema=args.schema,
                             target_database=args.target_database,
                             conversation_snapshot_path=args.conversation_snapshot,
                             expected_conversation_sha256=args.expected_conversation_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["verified"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
