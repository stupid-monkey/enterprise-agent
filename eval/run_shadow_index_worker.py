"""Consume an opt-in SQLite outbox into the dedicated local pgvector index."""

import argparse
import json
import os
import time
from pathlib import Path

from sentence_transformers import SentenceTransformer

from app.api.documents import DocumentRepository
from app.api.pg_hybrid_search import PgHybridSearch
from app.api.shadow_sync import drain_index_outbox
from eval.evaluate_pg_hybrid import MODEL_REVISION


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sqlite", required=True, type=Path)
    parser.add_argument("--dsn", default=os.getenv("RETRIEVAL_DATABASE_URL",
                        "postgresql://agent_retrieval:local-retrieval-only@127.0.0.1:55432/agent_retrieval"))
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--rebuild", action="store_true",
                        help="Queue every current document after an index reset")
    parser.add_argument("--watch", action="store_true", help="Keep polling after a batch")
    args = parser.parse_args()
    if not args.source_sqlite.is_file():
        parser.error("source SQLite file does not exist")
    if args.batch_size < 1 or args.poll_seconds <= 0 or args.max_attempts < 1:
        parser.error("batch size, poll interval and max attempts must be positive")
    model = SentenceTransformer("BAAI/bge-small-zh", revision=MODEL_REVISION,
                                local_files_only=True)
    repository = DocumentRepository(args.source_sqlite, record_index_events=True)
    try:
        pg = PgHybridSearch(args.dsn)
        try:
            pg.initialize()
            pg.bind_source(repository.index_source_id, MODEL_REVISION, rebuild=args.rebuild)
            if args.rebuild:
                print(json.dumps({"queued_rebuild": repository.queue_index_rebuild()}), flush=True)
            while True:
                result = drain_index_outbox(repository, pg, model, max_events=args.batch_size,
                                            max_attempts=args.max_attempts)
                if any(result.values()):
                    print(json.dumps(result), flush=True)
                if not args.watch:
                    break
                time.sleep(args.poll_seconds)
        finally:
            pg.close()
    finally:
        repository.close()


if __name__ == "__main__":
    main()
