"""Local cold/hot authorized-search benchmark with optional synthetic scale."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from itertools import cycle, islice
import json
import platform
import time
from pathlib import Path
from uuid import uuid4

from redis import Redis

from app.api.documents import DocumentRepository
from app.api.search_cache import RedisSearchCache
from app.security.policy import Classification, Subject


QUESTIONS = ("项目预算审批流程", "客户合同归档要求", "产品上线风险检查", "员工培训考核标准")


def add_synthetic_documents(repository: DocumentRepository, count: int) -> None:
    with repository._lock, repository._db:
        for index in range(count):
            topic = QUESTIONS[index % len(QUESTIONS)]
            document_id = f"benchmark-{index:06d}"
            content = "\n\n".join(
                f"{topic}：第 {index} 份资料的第 {section} 节，记录责任人、处理步骤和完成时限。"
                for section in range(3)
            )
            repository._db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (document_id, "demo-org", f"{topic} {index}", "TXT", "基准资料", "v1.0",
                 "2026-10-29", "benchmark", "正文", content[:500], "published", "INTERNAL",
                 "organization", "[]", "[]", "[]"),
            )
            repository._index_document(document_id, f"{topic} {index}", content, "正文")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3)


def measure(repository: DocumentRepository, subject: Subject,
            questions: list[str], workers: int = 1
            ) -> tuple[list[float], list[list[tuple[str, str, str]]], float]:
    def one(question: str) -> tuple[float, list[tuple[str, str, str]]]:
        started = time.perf_counter()
        hits = repository.search_visible(subject, question, limit=3)
        latency = (time.perf_counter() - started) * 1000
        return latency, [(hit.document.document_id, hit.location, hit.text) for hit in hits]

    started = time.perf_counter()
    if workers == 1:
        samples = list(map(one, questions))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            samples = list(pool.map(one, questions))
    elapsed = time.perf_counter() - started
    return [sample[0] for sample in samples], [sample[1] for sample in samples], elapsed


def summary(values: list[float], elapsed: float) -> dict[str, float]:
    return {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95),
            "throughput_rps": round(len(values) / elapsed, 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6382/0")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--documents", type=int, default=0,
                        help="Generated documents; 0 uses the small built-in demo catalog")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--requests", type=int, default=100)
    args = parser.parse_args()
    if args.documents < 0 or args.workers < 1 or args.requests < 4:
        parser.error("documents must be nonnegative, workers positive, and requests at least 4")
    client = Redis.from_url(args.redis_url, decode_responses=True, socket_timeout=1)
    client.ping()
    cache = RedisSearchCache(args.redis_url, client=client,
                             key_prefix=f"agent:search:benchmark:{uuid4().hex}:")
    plain = DocumentRepository(":memory:", seed_demo=args.documents == 0)
    cached = DocumentRepository(":memory:", seed_demo=args.documents == 0, search_cache=cache)
    subject = Subject("benchmark-employee", "demo-org", frozenset({"general"}),
                      Classification.INTERNAL)
    if args.documents:
        add_synthetic_documents(plain, args.documents)
        add_synthetic_documents(cached, args.documents)
        query_set = QUESTIONS
    else:
        query_set = ("差旅报销审批流程", "员工年假标准", "报销到账时间", "产品发布检查")
    queries = list(islice(cycle(query_set), args.requests))
    try:
        baseline, baseline_results, baseline_elapsed = measure(plain, subject, queries, args.workers)
        cold, cold_results, cold_elapsed = measure(cached, subject, list(query_set))
        hot, hot_results, hot_elapsed = measure(cached, subject, queries, args.workers)
        if cold_results != baseline_results[:4] or hot_results != baseline_results:
            raise AssertionError("Cached authorized search differs from uncached results")
        counts = cache.stats()
        report = {"dataset": "seed_demo" if args.documents == 0 else "generated synthetic documents",
                  "documents": plain._db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                  "segments": plain._db.execute("SELECT COUNT(*) FROM document_segments").fetchone()[0],
                  "requests": len(queries), "workers": args.workers,
                  "unique_queries": len(query_set), "python": platform.python_version(),
                  "platform": platform.platform(), "processor": platform.processor(),
                  "redis_version": client.info("server")["redis_version"],
                  "correctness": "cold/hot results equal uncached results",
                  "baseline_ms": summary(baseline, baseline_elapsed),
                  "cold_ms": summary(cold, cold_elapsed),
                  "hot_ms": summary(hot, hot_elapsed),
                  "cache": counts, "hit_rate": round(counts["hits"] / (counts["hits"] + counts["misses"]), 3),
                  "limitations": "In-memory SQLite, one shared connection/lock, four repeated query types, one local process; excludes HTTP, model time and realistic query distribution. Not a production SLA."}
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
    finally:
        plain.close()
        cached.close()


if __name__ == "__main__":
    main()
