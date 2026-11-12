"""Local synthetic SQLite/PostgreSQL profile operation latency comparison."""

import argparse
import json
import os
import statistics
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.api.pg_user_memories import PgUserMemoryRepository
from app.api.user_memories import UserMemoryRepository
from app.security.policy import Classification, Subject


def percentile(values: list[float], p: float) -> float:
    values = sorted(values)
    return round(values[max(0, int((len(values) * p + 0.999999)) - 1)], 3)


def measure(repository, n: int) -> dict:
    timings = {"write": [], "read": [], "delete": []}
    subjects = [Subject(f"bench-user-{index}", "bench-org", frozenset(),
                        Classification.INTERNAL) for index in range(n + 10)]
    for index, subject in enumerate(subjects):
        samples = {} if index < 10 else timings
        before = time.perf_counter()
        repository.upsert(subject, "role", "测试工程师")
        if samples is timings:
            samples["write"].append((time.perf_counter() - before) * 1000)
        before = time.perf_counter()
        rows = repository.list_for(subject)
        if len(rows) != 1 or rows[0]["value"] != "测试工程师":
            raise RuntimeError("PROFILE_READ_MISMATCH")
        if samples is timings:
            samples["read"].append((time.perf_counter() - before) * 1000)
        before = time.perf_counter()
        repository.delete(subject, "role")
        if repository.list_for(subject):
            raise RuntimeError("PROFILE_DELETE_MISMATCH")
        if samples is timings:
            samples["delete"].append((time.perf_counter() - before) * 1000)
    return {name: {"p50_ms": round(statistics.median(values), 3),
                   "p95_ms": percentile(values, 0.95),
                   "max_ms": round(max(values), 3)}
            for name, values in timings.items()}


def benchmark(dsn: str, *, operations: int = 100) -> dict:
    if conninfo_to_dict(dsn).get("dbname") != "agent_memory_lab":
        raise ValueError("MEMORY_LAB_DATABASE_REQUIRED")
    if not 10 <= operations <= 500:
        raise ValueError("OPERATION_COUNT_OUT_OF_RANGE")
    schema = "memory_test_" + uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="memory_repository_bench_") as directory:
        sqlite = UserMemoryRepository(Path(directory) / "memory.sqlite3")
        pg = PgUserMemoryRepository(dsn, schema=schema)
        try:
            pg.create_schema()
            return {"dataset": "synthetic-memory-repository-v1",
                    "operations_per_type": operations,
                    "sqlite": measure(sqlite, operations),
                    "postgres": measure(pg, operations)}
        finally:
            sqlite.close()
            pg.connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)))
            pg.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dsn = os.getenv("MEMORY_TEST_DATABASE_URL", "")
    if not dsn:
        raise ValueError("MEMORY_TEST_DATABASE_URL_REQUIRED")
    result = benchmark(dsn, operations=args.operations)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
