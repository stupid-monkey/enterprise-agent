"""Import one offline snapshot into a disposable lab schema and remove it."""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql

from app.api.pg_conversations import PgConversationRepository
from eval.migrate_sqlite_conversations import apply_snapshot, source_snapshot


def evaluate(path: Path, dsn: str) -> dict:
    _, summary, source_hash = source_snapshot(path)
    schema = "conversation_test_" + uuid4().hex[:12]
    try:
        imported = apply_snapshot(path, dsn, schema=schema,
                                  expected_sha256=source_hash)
        repository = PgConversationRepository(dsn, schema=schema)
        try:
            self_check = repository.healthy()
        finally:
            repository.close()
        return {"dataset": "offline-conversation-import-v1",
                "source_sha256": source_hash,
                "tables": summary,
                "import_matches_source": imported["tables"] == summary,
                "repository_healthy": self_check,
                "verified": imported["verified"] and self_check}
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE")
                               .format(sql.Identifier(schema)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dsn = os.getenv("MEMORY_TEST_DATABASE_URL", "")
    if not dsn:
        raise ValueError("MEMORY_TEST_DATABASE_URL_REQUIRED")
    result = evaluate(args.snapshot, dsn)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
