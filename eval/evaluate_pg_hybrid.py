"""Offline-model evaluation of the isolated pgvector shadow index."""

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

from sentence_transformers import SentenceTransformer

from app.api.documents import DocumentRepository
from app.api.hybrid_evidence import calibrate_distance_gate, resolve_gated_candidates
from app.api.pg_hybrid_search import PgHybridSearch
from app.api.shadow_sync import document_snapshot, drain_index_outbox
from app.api.workflow_answers import grounded_extract
from eval.evaluate_authorized_search import SUBJECTS
from app.security.policy import Visibility
from app.workflow.graph import Evidence


CASES = Path(__file__).with_name("hybrid_search_cases.json")
MODEL_REVISION = "1d2363c5de6ce9ba9c890c8e23a4c72dce540ca8"


def semantic_source_quote(model: SentenceTransformer, question_vector, text: str) -> str | None:
    """Pick an exact source sentence by embedding similarity, without rewriting it."""
    sentences = [match.group().strip() for match in re.finditer(r"[^。！？!?；;]+[。！？!?；;]?", text)
                 if match.group().strip()]
    if not sentences:
        return None
    vectors = model.encode(sentences, normalize_embeddings=True, show_progress_bar=False)
    best = max(range(len(sentences)), key=lambda index: float(vectors[index] @ question_vector))
    return sentences[best]


def evaluate(dsn: str) -> dict:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    model = SentenceTransformer("BAAI/bge-small-zh", revision=MODEL_REVISION,
                                local_files_only=True)
    repository = DocumentRepository(":memory:", seed_demo=True, record_index_events=True)
    pg = PgHybridSearch(dsn)
    try:
        pg.initialize()
        pg.bind_source(repository.index_source_id, MODEL_REVISION, rebuild=True)
        try:
            pg.bind_source("different-source", MODEL_REVISION)
        except ValueError:
            source_guard = True
        else:
            source_guard = False
        if not source_guard:
            raise AssertionError("Shadow index accepted another source")
        bootstrap = drain_index_outbox(repository, pg, model)
        if bootstrap != {"processed": 6, "skipped_old": 0, "failed": 0}:
            raise AssertionError(f"Shadow bootstrap failed: {bootstrap}")
        vectors = model.encode([case["question"] for case in cases],
                               normalize_embeddings=True, show_progress_bar=False)
        results = []
        raw_candidates = []
        for case, vector in zip(cases, vectors):
            subject = SUBJECTS[case["role"]]
            started = time.perf_counter()
            lexical = repository.search_visible(subject, case["question"], area=case.get("area"), limit=3)
            lexical_ms = round((time.perf_counter() - started) * 1000, 3)
            started = time.perf_counter()
            hybrid = pg.search(subject, case["question"], vector, area=case.get("area"), limit=3)
            raw_candidates.append(hybrid)
            hybrid_ms = round((time.perf_counter() - started) * 1000, 3)
            visible = {row.document_id for row in repository.list_visible(subject)}
            lexical_ids = [hit.document.document_id for hit in lexical]
            hybrid_ids = [hit["document_id"] for hit in hybrid]
            expected = case["expected_id"]
            results.append({"case": case["name"], "expected_id": expected,
                            "split": case["split"],
                            "lexical_ids": lexical_ids, "hybrid_ids": hybrid_ids,
                            "hybrid_scores": [{"document_id": hit["document_id"],
                                               "vector_distance": round(hit["vector_distance"], 4),
                                               "lexical_score": round(float(hit["lexical_score"]), 4),
                                               "score": round(float(hit["score"]), 5)} for hit in hybrid],
                            "lexical_recall_at_3": expected in lexical_ids if expected else None,
                            "hybrid_recall_at_3": expected in hybrid_ids if expected else None,
                            "hybrid_unauthorized": [item for item in hybrid_ids if item not in visible],
                            "lexical_ms": lexical_ms, "hybrid_ms": hybrid_ms})
        positives = [row for row in results if row["expected_id"]]
        negatives = [row for row in results if not row["expected_id"]]
        threshold = calibrate_distance_gate([row for row in results if row["split"] == "calibration"])
        for row, case, candidates, vector in zip(results, cases, raw_candidates, vectors):
            gated = resolve_gated_candidates(repository, SUBJECTS[case["role"]],
                                             candidates, max_vector_distance=threshold,
                                             area=case.get("area"), limit=1)
            row["gated_id"] = gated[0].document.document_id if gated else None
            row["gated_correct"] = row["gated_id"] == row["expected_id"]
            if gated:
                hit = gated[0]
                evidence = Evidence(document_id=hit.document.document_id,
                                    version_id=hit.document.version,
                                    location=hit.location, text=hit.text)
                quote = grounded_extract(case["question"], evidence, hit.document.title)
            else:
                quote = None
            row["extractive_quote"] = quote
            row["quote_is_source_substring"] = bool(quote and quote in gated[0].text) if gated else quote is None
            row["expected_fact_in_quote"] = (case["expected_fact"] in quote if quote else False) \
                if case["expected_id"] else quote is None
            semantic_quote = semantic_source_quote(model, vector, gated[0].text) if gated else None
            row["semantic_quote"] = semantic_quote
            row["semantic_quote_is_source_substring"] = (
                bool(semantic_quote and semantic_quote in gated[0].text) if gated else semantic_quote is None)
            row["expected_fact_in_semantic_quote"] = (
                case["expected_fact"] in semantic_quote if semantic_quote else False
            ) if case["expected_id"] else semantic_quote is None
            fallback_quote = quote or semantic_quote
            row["fallback_quote"] = fallback_quote
            row["fallback_quote_is_source_substring"] = (
                bool(fallback_quote and fallback_quote in gated[0].text) if gated else fallback_quote is None)
            row["expected_fact_in_fallback_quote"] = (
                case["expected_fact"] in fallback_quote if fallback_quote else False
            ) if case["expected_id"] else fallback_quote is None
        employee = SUBJECTS["employee"]
        travel_vector = vectors[0]
        security_checks = {}
        pg.connection.execute("UPDATE retrieval_documents SET denied_user_ids = ARRAY[%s] WHERE document_id = '1'",
                              (employee.user_id,))
        security_checks["revocation"] = "1" not in {
            row["document_id"] for row in pg.search(employee, cases[0]["question"], travel_vector)}
        pg.connection.execute("UPDATE retrieval_documents SET denied_user_ids = '{}' WHERE document_id = '1'")
        pg.connection.execute("UPDATE retrieval_documents SET state = 'review' WHERE document_id = '1'")
        security_checks["unpublished"] = "1" not in {
            row["document_id"] for row in pg.search(employee, cases[0]["question"], travel_vector)}
        pg.connection.execute("UPDATE retrieval_documents SET state = 'published' WHERE document_id = '1'")
        sync_checks = {}
        manager = SUBJECTS["manager"]
        content = "采购合同审批由部门负责人提交，财务主管核对金额后归档。"
        uploaded_id = repository.create_upload(
            manager, title="采购合同审批流程", filename="procurement.txt", file_type="TXT",
            area="制度规范", visibility=Visibility.ORGANIZATION,
            content=content.encode(), extracted_text=content)
        question = "采购合同审批如何归档？"
        query_vector = model.encode(question, normalize_embeddings=True)
        def matching() -> list[dict]:
            return [candidate for candidate in pg.search(employee, question, query_vector, limit=10)
                    if candidate["document_id"] == uploaded_id]
        sync_checks["unpublished_not_indexed"] = not matching()
        drain_index_outbox(repository, pg, model)
        sync_checks["unpublished_hidden"] = not matching()
        repository.publish(manager, uploaded_id)
        sync_checks["publication_before_sync_hidden"] = not matching()
        drain_index_outbox(repository, pg, model)
        old_candidate = matching()
        sync_checks["published_visible"] = bool(old_candidate)
        repository.set_denied("demo-org", uploaded_id, employee.user_id, True)
        sync_checks["stale_acl_rechecked"] = not resolve_gated_candidates(
            repository, employee, old_candidate, max_vector_distance=2)
        drain_index_outbox(repository, pg, model)
        sync_checks["revocation_synced"] = not matching()
        repository.set_denied("demo-org", uploaded_id, employee.user_id, False)
        drain_index_outbox(repository, pg, model)
        old_document, old_segments = document_snapshot(repository, model, uploaded_id)
        old_event_id = pg.connection.execute(
            "SELECT index_event_id FROM retrieval_documents WHERE document_id = %s",
            (uploaded_id,)).fetchone()["index_event_id"]
        revised = "采购合同先由部门负责人审批，再由财务主管确认并归档。"
        repository.create_revision(manager, uploaded_id, filename="procurement.txt",
                                   file_type="TXT", content=revised.encode(), extracted_text=revised)
        repository.publish_revision(manager, uploaded_id)
        sync_checks["stale_version_rechecked"] = not resolve_gated_candidates(
            repository, employee, old_candidate, max_vector_distance=2)
        drain_index_outbox(repository, pg, model)
        updated = matching()
        sync_checks["revision_synced"] = bool(updated) and all(
            candidate["version"] == "v1.1" and candidate["body"] == revised for candidate in updated)
        sync_checks["old_event_cannot_overwrite_new"] = (
            not pg.upsert_document(old_document, old_segments, event_id=old_event_id)
            and all(candidate["version"] == "v1.1" and candidate["body"] == revised
                    for candidate in matching()))
        other = PgHybridSearch(dsn)
        try:
            other.initialize()
            other.bind_source("different-source", MODEL_REVISION, rebuild=True)
            try:
                pg.upsert_document(old_document, old_segments, event_id=old_event_id + 1000)
            except RuntimeError:
                sync_checks["old_worker_rejected_after_rebind"] = True
            else:
                sync_checks["old_worker_rejected_after_rebind"] = False
            other.bind_source(repository.index_source_id, MODEL_REVISION, rebuild=True)
        finally:
            other.close()
        rebuild_queued = repository.queue_index_rebuild()
        rebuild_result = drain_index_outbox(repository, pg, model, max_events=20)
        sync_checks["rebuild_restores_index"] = (rebuild_queued == 7
            and rebuild_result == {"processed": 7, "skipped_old": 0, "failed": 0}
            and bool(matching()))
        if not all(security_checks.values()) or any(row["hybrid_unauthorized"] for row in results):
            raise AssertionError("SQL authorization failed")
        if not all(sync_checks.values()):
            raise AssertionError(f"Shadow sync failed: {sync_checks}")
        return {"dataset": CASES.name,
                "dataset_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
                "model": "BAAI/bge-small-zh local 512d",
                "model_revision": MODEL_REVISION,
                "documents": 6, "segments": 6, "cases": len(cases),
                "gate": {"rule": "top_candidate_vector_distance_at_most",
                         "threshold": threshold, "calibrated_on": "calibration split only"},
                "calibration_cases": sum(row["split"] == "calibration" for row in results),
                "calibration_gated_correct": sum(row["gated_correct"] for row in results
                                                 if row["split"] == "calibration"),
                "holdout_cases": sum(row["split"] == "holdout" for row in results),
                "holdout_gated_correct": sum(row["gated_correct"] for row in results
                                             if row["split"] == "holdout"),
                "holdout_positive_recall": sum(row["gated_correct"] for row in results
                                               if row["split"] == "holdout" and row["expected_id"]),
                "holdout_positive_cases": sum(row["split"] == "holdout" and bool(row["expected_id"])
                                              for row in results),
                "holdout_negative_rejections": sum(row["gated_correct"] for row in results
                                                   if row["split"] == "holdout" and not row["expected_id"]),
                "holdout_negative_cases": sum(row["split"] == "holdout" and not row["expected_id"]
                                              for row in results),
                "holdout_answer_fact_supported": sum(row["expected_fact_in_quote"] for row in results
                                                     if row["split"] == "holdout" and row["expected_id"]),
                "holdout_quote_source_supported": sum(row["quote_is_source_substring"] for row in results
                                                      if row["split"] == "holdout"),
                "holdout_semantic_fact_supported": sum(row["expected_fact_in_semantic_quote"]
                                                        for row in results
                                                        if row["split"] == "holdout" and row["expected_id"]),
                "holdout_semantic_quote_source_supported": sum(
                    row["semantic_quote_is_source_substring"] for row in results
                    if row["split"] == "holdout"),
                "holdout_fallback_fact_supported": sum(row["expected_fact_in_fallback_quote"]
                                                       for row in results
                                                       if row["split"] == "holdout" and row["expected_id"]),
                "holdout_fallback_quote_source_supported": sum(
                    row["fallback_quote_is_source_substring"] for row in results
                    if row["split"] == "holdout"),
                "lexical_recall_at_3": sum(row["lexical_recall_at_3"] for row in positives),
                "hybrid_recall_at_3": sum(row["hybrid_recall_at_3"] for row in positives),
                "positive_cases": len(positives),
                "unauthorized_hits": sum(len(row["hybrid_unauthorized"]) for row in results),
                "negative_cases_with_results": sum(bool(row["hybrid_ids"]) for row in negatives),
                "negative_cases": len(negatives), "security_checks": security_checks,
                "bootstrap_events": bootstrap,
                "source_guard": source_guard,
                "sync_checks": sync_checks,
                "results": results,
                "limitations": "Tiny synthetic set; threshold is only a candidate gate, not proof that an answer is supported. Timings exclude embedding generation. Shadow index only; no API cutover."}
    finally:
        pg.close()
        repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.getenv("RETRIEVAL_DATABASE_URL",
                        "postgresql://agent_retrieval:local-retrieval-only@127.0.0.1:55432/agent_retrieval"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.dsn)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
