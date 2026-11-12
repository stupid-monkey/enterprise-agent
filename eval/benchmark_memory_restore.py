"""Restore an isolated nonempty synthetic memory schema and compare content hashes.

Requires a running Docker `memory-db` and the dedicated agent_memory_lab DSN.
Creates and removes only randomized synthetic schema/database names.
"""

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.api.pg_user_memories import PgUserMemoryRepository
from app.security.policy import Classification, Subject


TABLES = {
    "facts": "SELECT org_id, owner_id, kind, value, origin, source_conversation_id, "
             "created_at, updated_at, expires_at, version FROM memory_fact "
             "ORDER BY org_id, owner_id, kind",
    "sources": "SELECT org_id, owner_id, id, state FROM memory_source "
               "ORDER BY org_id, owner_id, id",
    "settings": "SELECT org_id, owner_id, auto_suggest FROM memory_setting "
                "ORDER BY org_id, owner_id",
}


def snapshot(repository: PgUserMemoryRepository) -> dict:
    result = {}
    for name, query in TABLES.items():
        rows = [dict(row) for row in repository.connection.execute(query)]
        encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                             default=str, separators=(",", ":")).encode()
        result[name] = {"count": len(rows), "sha256": hashlib.sha256(encoded).hexdigest()}
    return result


def docker(*arguments: str) -> None:
    subprocess.run(["docker", "exec", "enterprise-agent-memory-db-1", *arguments],
                   check=True, capture_output=True, text=True)


def benchmark(dsn: str, *, facts: int = 50) -> dict:
    parameters = conninfo_to_dict(dsn)
    if parameters.get("dbname") != "agent_memory_lab":
        raise ValueError("MEMORY_LAB_DATABASE_REQUIRED")
    if not 1 <= facts <= 500:
        raise ValueError("FACT_COUNT_OUT_OF_RANGE")
    identifier = uuid4().hex[:12]
    schema = "memory_test_restore_" + identifier
    restored_name = "memory_restore_eval_" + identifier
    archive = "/tmp/" + schema + ".dump"
    source = PgUserMemoryRepository(dsn, schema=schema)
    restored = None
    database_created = False
    try:
        source.create_schema()
        for index in range(facts):
            subject = Subject(f"synthetic-{index}", "synthetic-org", frozenset(),
                              Classification.INTERNAL)
            source.upsert(subject, "role", f"合成岗位{index}")
            source.register_source(subject, f"synthetic-source-{index}")
            source.set_auto_suggest(subject, index % 2 == 0)
        expected = snapshot(source)
        started = time.perf_counter()
        docker("pg_dump", "-U", "agent_memory_lab", "-d", "agent_memory_lab",
               "-n", schema, "-Fc", "-f", archive)
        backup_seconds = time.perf_counter() - started
        docker("createdb", "-U", "agent_memory_lab", "-O", "agent_memory_lab",
               restored_name)
        database_created = True
        started = time.perf_counter()
        docker("pg_restore", "-U", "agent_memory_lab", "-d", restored_name, archive)
        restore_seconds = time.perf_counter() - started
        restored_dsn = make_conninfo(**{**parameters, "dbname": restored_name})
        restored = PgUserMemoryRepository(restored_dsn, schema=schema)
        actual = snapshot(restored)
        versions = restored.connection.execute(
            "SELECT version FROM memory_queue_schema_version").fetchall()
        verified = expected == actual and [row["version"] for row in versions] == [6]
        if not verified:
            raise RuntimeError("RESTORED_MEMORY_CONTENT_MISMATCH")
        return {"dataset": "synthetic-memory-restore-v1", "facts": facts,
                "backup_seconds": round(backup_seconds, 3),
                "restore_seconds": round(restore_seconds, 3),
                "queue_schema_version": 6, "checks": {
                    name: {"source_count": expected[name]["count"],
                           "restored_count": actual[name]["count"],
                           "content_sha256_match": expected[name]["sha256"] == actual[name]["sha256"]}
                    for name in TABLES}, "verified": verified}
    finally:
        if restored is not None:
            restored.close()
        if database_created:
            docker("dropdb", "-U", "agent_memory_lab", restored_name)
        source.connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
            sql.Identifier(schema)))
        source.close()
        try:
            docker("rm", "-f", archive)
        except subprocess.CalledProcessError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=int, default=50)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dsn = os.getenv("MEMORY_TEST_DATABASE_URL", "")
    if not dsn:
        raise ValueError("MEMORY_TEST_DATABASE_URL_REQUIRED")
    result = benchmark(dsn, facts=args.facts)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
