"""Synthetic-only SQLite outbox -> PostgreSQL memory relay benchmark.

Requires the dedicated agent_memory_lab database. Creates a random schema and
temporary SQLite file, then removes both. Never reads the product databases.
"""

import argparse
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from math import ceil
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.api.conversations import ConversationRepository
from app.api.memory_delete_relay import MemoryDeleteRelayWorker
from app.api.pg_user_memories import PgUserMemoryRepository
from app.security.policy import Classification, Subject


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, ceil(len(ordered) * fraction) - 1)], 3)


def benchmark(dsn: str, *, events: int = 50, fail_first: bool = False,
              workers: int = 1, target_outage_seconds: float = 0) -> dict:
    if not 1 <= events <= 200:
        raise ValueError("EVENT_COUNT_OUT_OF_RANGE")
    if not 1 <= workers <= 8:
        raise ValueError("WORKER_COUNT_OUT_OF_RANGE")
    if not 0 <= target_outage_seconds <= 10:
        raise ValueError("TARGET_OUTAGE_OUT_OF_RANGE")
    if conninfo_to_dict(dsn).get("dbname") != "agent_memory_lab":
        raise ValueError("MEMORY_LAB_DATABASE_REQUIRED")
    schema = "memory_test_" + uuid4().hex[:12]
    pg = PgUserMemoryRepository(dsn, schema=schema)
    try:
        pg.create_schema()
        with tempfile.TemporaryDirectory(prefix="memory_relay_synthetic_") as directory:
            source_path = Path(directory) / "synthetic-conversations.sqlite3"
            source = ConversationRepository(source_path, enable_delete_outbox=True)
            try:
                sources = []
                for index in range(events):
                    subject = Subject(f"synthetic-{index}", "synthetic-org", frozenset(),
                                      Classification.INTERNAL)
                    conversation_id = source.append(
                        subject, None, "合成对话", "general", {"status": "ok"})
                    pg.register_source(subject, conversation_id)
                    pg.set_auto_suggest(subject, True)
                    question = "我是测试工程师"
                    job_id = pg.enqueue_suggestion(subject, question, conversation_id)
                    claim = pg.claim_suggestion_job()
                    if not claim or claim["id"] != job_id:
                        raise RuntimeError("SYNTHETIC_JOB_CLAIM_FAILED")
                    if pg.apply_auto_fact(job_id, claim["lease_token"], "role", question) is None:
                        raise RuntimeError("SYNTHETIC_FACT_WRITE_FAILED")
                    pg.finish_suggestion_job(job_id, claim["lease_token"])
                    sources.append((subject, conversation_id))
                for subject, conversation_id in sources:
                    if not source.delete(subject, conversation_id):
                        raise RuntimeError("SYNTHETIC_SOURCE_DELETE_FAILED")

                factory_calls = 0
                result_lock = Lock()
                stop = Event()
                durations: list[float] = []
                outcomes: dict[str, int] = {}
                worker_deliveries = [0] * workers
                worker_sources = [ConversationRepository(source_path, enable_delete_outbox=True)
                                  for _ in range(workers)]
                start = monotonic()
                outage_until = start + target_outage_seconds

                def factory() -> PgUserMemoryRepository:
                    nonlocal factory_calls
                    with result_lock:
                        factory_calls += 1
                        call_number = factory_calls
                    if fail_first and call_number == 1:
                        raise psycopg.OperationalError("synthetic target outage")
                    if monotonic() < outage_until:
                        raise psycopg.OperationalError("synthetic timed target outage")
                    return PgUserMemoryRepository(dsn, schema=schema)

                def consume(worker_index: int, repository: ConversationRepository) -> None:
                    worker = MemoryDeleteRelayWorker(repository, factory, poll_seconds=0.01)
                    while not stop.is_set():
                        began = monotonic()
                        outcome = worker.run_once()
                        with result_lock:
                            outcomes[outcome] = outcomes.get(outcome, 0) + 1
                            if outcome == "delivered":
                                durations.append((monotonic() - began) * 1000)
                                worker_deliveries[worker_index] += 1
                        if outcome == "idle":
                            stop.wait(0.01)

                try:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = [pool.submit(consume, index, repository)
                                   for index, repository in enumerate(worker_sources)]
                        try:
                            deadline = start + 45 + target_outage_seconds
                            while source.pending_delete_count() and monotonic() < deadline:
                                for future in futures:
                                    if future.done():
                                        future.result()
                                sleep(0.01)
                        finally:
                            stop.set()
                        for future in futures:
                            future.result(timeout=10)
                finally:
                    stop.set()
                    for repository in worker_sources:
                        repository.close()
                elapsed = monotonic() - start
                if source.pending_delete_count():
                    raise RuntimeError("SYNTHETIC_RELAY_DID_NOT_DRAIN")
                rows = source._db.execute("""SELECT created_at, delivered_at, attempts
                    FROM conversation_delete_outbox""").fetchall()
                propagation = [
                    (datetime.fromisoformat(row["delivered_at"])
                     - datetime.fromisoformat(row["created_at"])).total_seconds() * 1000
                    for row in rows]
                if (len(propagation) != events or len(durations) != events
                        or sum(worker_deliveries) != events):
                    raise RuntimeError("SYNTHETIC_RELAY_COUNT_MISMATCH")
                target = pg.connection.execute("""SELECT
                    (SELECT count(*) FROM memory_fact) AS facts,
                    (SELECT count(*) FROM memory_source WHERE state = 'deleted') AS tombstones""").fetchone()
                if target["facts"] != 0 or target["tombstones"] != events:
                    raise RuntimeError("SYNTHETIC_RELAY_TARGET_MISMATCH")
                return {
                    "dataset": "local-synthetic-memory-delete-relay-v1",
                    "events": events,
                    "workers": workers,
                    "injected_initial_failure": fail_first,
                    "target_outage_seconds": target_outage_seconds,
                    "outcomes": outcomes,
                    "worker_deliveries": worker_deliveries,
                    "elapsed_seconds": round(elapsed, 3),
                    "throughput_events_per_second": round(events / elapsed, 2),
                    "service_ms_p50": percentile(durations, 0.5),
                    "service_ms_p95": percentile(durations, 0.95),
                    "propagation_ms_p50": percentile(propagation, 0.5),
                    "propagation_ms_p95": percentile(propagation, 0.95),
                    "propagation_ms_max": round(max(propagation), 3),
                    "retried_events": sum(row["attempts"] > 1 for row in rows),
                    "pending_at_end": 0,
                    "remaining_facts": 0,
                    "deleted_source_tombstones": events,
                }
            finally:
                source.close()
    finally:
        pg.connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
            sql.Identifier(schema)))
        pg.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=50)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--fail-first", action="store_true")
    parser.add_argument("--target-outage-seconds", type=float, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dsn = os.getenv("MEMORY_TEST_DATABASE_URL", "")
    if not dsn:
        raise ValueError("MEMORY_TEST_DATABASE_URL_REQUIRED")
    report = benchmark(dsn, events=args.events, fail_first=args.fail_first,
                       workers=args.workers,
                       target_outage_seconds=args.target_outage_seconds)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
