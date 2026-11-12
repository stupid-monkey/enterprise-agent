"""Synthetic multi-process SQLite outbox -> PostgreSQL enqueue/delete benchmark."""

import argparse
import json
import os
import tempfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.api.conversations import ConversationRepository
from app.api.memory_delete_relay import MemoryDeleteRelayWorker
from app.api.memory_enqueue_relay import MemoryEnqueueRelayWorker
from app.api.pg_user_memories import PgUserMemoryRepository
from app.api.postgres_memory_service import PostgresMemoryService
from app.security.policy import Classification, Subject


def consume(path: str, dsn: str, schema: str, phase: str) -> dict[str, int]:
    conversations = ConversationRepository(path, enable_delete_outbox=True)
    memories = None
    try:
        if phase == "enqueue":
            memories = PostgresMemoryService(dsn, conversations, schema=schema,
                                             default_auto_suggest=True)
            worker = MemoryEnqueueRelayWorker(conversations, memories, poll_seconds=0.02)
        else:
            worker = MemoryDeleteRelayWorker(
                conversations, lambda: PgUserMemoryRepository(dsn, schema=schema),
                poll_seconds=0.02)
        outcomes = Counter()
        idle = 0
        for _ in range(2000):
            result = worker.run_once()
            outcomes[result] += 1
            idle = idle + 1 if result == "idle" else 0
            if idle >= 10:
                break
            if result == "idle":
                time.sleep(0.02)
        return dict(outcomes)
    finally:
        if memories is not None:
            memories.close()
        conversations.close()


def benchmark(dsn: str, *, events: int = 100, processes: int = 4) -> dict:
    if conninfo_to_dict(dsn).get("dbname") != "agent_memory_lab":
        raise ValueError("MEMORY_LAB_DATABASE_REQUIRED")
    if not 1 <= events <= 1000 or not 1 <= processes <= 8:
        raise ValueError("BENCHMARK_SIZE_OUT_OF_RANGE")
    schema = "memory_test_" + uuid4().hex[:12]
    pg = PgUserMemoryRepository(dsn, schema=schema, default_auto_suggest=True)
    pg.create_schema()
    try:
        with tempfile.TemporaryDirectory(prefix="memory_processes_") as directory:
            path = str(Path(directory) / "conversations.sqlite3")
            conversations = ConversationRepository(path, enable_delete_outbox=True)
            sources = []
            for index in range(events):
                subject = Subject(f"synthetic-{index}", "synthetic-org", frozenset(),
                                  Classification.INTERNAL)
                source = conversations.append(
                    subject, None, "我是测试工程师", "general", {"status": "ok"},
                    memory_include_ordinary=True)
                sources.append((subject, source))
            results = {}
            for phase in ("enqueue", "delete"):
                if phase == "delete":
                    for subject, source in sources:
                        if not conversations.delete(subject, source):
                            raise RuntimeError("SOURCE_DELETE_FAILED")
                started = time.perf_counter()
                with ProcessPoolExecutor(max_workers=processes) as pool:
                    rows = list(pool.map(consume, [path] * processes, [dsn] * processes,
                                         [schema] * processes, [phase] * processes))
                elapsed = time.perf_counter() - started
                counts = Counter()
                for row in rows:
                    counts.update(row)
                pending = (conversations.pending_memory_event_count() if phase == "enqueue"
                           else conversations.pending_delete_count())
                if pending or counts["delivered"] != events:
                    raise RuntimeError(f"{phase.upper()}_PROCESS_DELIVERY_FAILED")
                results[phase] = {"delivered": counts["delivered"],
                                  "pending": pending, "elapsed_seconds": round(elapsed, 3),
                                  "throughput_events_per_second": round(events / elapsed, 2),
                                  "worker_deliveries": [row.get("delivered", 0) for row in rows]}
            row = pg.connection.execute("""SELECT
                (SELECT count(*) FROM memory_source WHERE state = 'deleted') AS tombstones,
                (SELECT count(*) FROM memory_job) AS jobs,
                (SELECT count(*) FROM memory_fact) AS facts""").fetchone()
            if row["tombstones"] != events or row["jobs"] or row["facts"]:
                raise RuntimeError("PROCESS_TARGET_STATE_MISMATCH")
            conversations.close()
            return {"dataset": "synthetic-memory-processes-v1", "events": events,
                    "processes": processes, "phases": results,
                    "target": dict(row)}
    finally:
        pg.connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
            sql.Identifier(schema)))
        pg.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=100)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dsn = os.getenv("MEMORY_TEST_DATABASE_URL", "")
    if not dsn:
        raise ValueError("MEMORY_TEST_DATABASE_URL_REQUIRED")
    result = benchmark(dsn, events=args.events, processes=args.processes)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
