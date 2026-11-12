"""Evaluate the frozen hybrid gate on documents absent from calibration."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from sentence_transformers import SentenceTransformer

from app.api.documents import DocumentRepository
from app.api.hybrid_evidence import resolve_gated_candidates
from app.api.pg_hybrid_search import PgHybridSearch
from app.api.shadow_sync import drain_index_outbox
from app.api.workflow_answers import grounded_extract
from app.security.policy import Classification, Subject, UserRole, Visibility
from app.workflow.graph import Evidence
from eval.evidence_coverage import evidence_coverage
from eval.structured_evidence import structured_evidence_check
from eval.evaluate_pg_hybrid import MODEL_REVISION, semantic_source_quote


ROOT = Path(__file__).parent
DOCUMENTS = ROOT / "hybrid_independent_documents.json"
CASES = ROOT / "hybrid_independent_cases.json"
HOLDOUT_DOCUMENTS = ROOT / "hybrid_holdout_documents.json"
HOLDOUT_CASES = ROOT / "hybrid_holdout_cases.json"
STRESS_DOCUMENTS = ROOT / "hybrid_stress_v3_documents.json"
STRESS_CASES = ROOT / "hybrid_stress_v3_cases.json"
SOURCE_GROUP_DOCUMENTS = ROOT / "hybrid_source_group_v4_documents.json"
SOURCE_GROUP_CASES = ROOT / "hybrid_source_group_v4_cases.json"
SOURCE_GROUP_V5_DOCUMENTS = ROOT / "hybrid_source_group_v5_documents.json"
SOURCE_GROUP_V5_CASES = ROOT / "hybrid_source_group_v5_cases.json"
FROZEN_DISTANCE = 0.2215
SUITES = {
    "independent-v1": (DOCUMENTS, CASES),
    "holdout-v2": (HOLDOUT_DOCUMENTS, HOLDOUT_CASES),
    "stress-v3": (STRESS_DOCUMENTS, STRESS_CASES),
    "source-group-v4": (SOURCE_GROUP_DOCUMENTS, SOURCE_GROUP_CASES),
    "source-group-v5": (SOURCE_GROUP_V5_DOCUMENTS, SOURCE_GROUP_V5_CASES),
}


def subjects() -> dict[str, Subject]:
    return {
        "employee": Subject("employee-001", "demo-org", frozenset({"general"}),
                            Classification.INTERNAL),
        "product": Subject("product-owner", "demo-org", frozenset({"product"}),
                           Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER),
        "finance": Subject("finance-owner", "demo-org", frozenset({"finance"}),
                           Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER),
        "operations": Subject("operations-owner", "demo-org", frozenset({"operations"}),
                              Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER),
        "admin": Subject("admin-001", "demo-org", frozenset({"it"}),
                         Classification.RESTRICTED, role=UserRole.SYSTEM_ADMIN),
        "outsider": Subject("outside-owner", "other-org", frozenset({"general"}),
                            Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER),
    }


def load_documents(repository: DocumentRepository, rows: list[dict], identities: dict[str, Subject]) -> dict[str, str]:
    owners = {("demo-org", "general"): "product", ("demo-org", "product"): "product",
              ("demo-org", "finance"): "finance", ("demo-org", "operations"): "operations",
              ("other-org", "general"): "outsider"}
    document_ids = {}
    for item in rows:
        owner = identities[owners[(item["org"], item["department"])]]
        content = item["text"].encode("utf-8")
        document_id = repository.create_upload(
            owner, title=item["title"], filename=f"{item['key']}.txt", file_type="TXT",
            area=item["area"], visibility=Visibility(item["visibility"]),
            content=content, extracted_text=item["text"])
        if item["published"] and not repository.publish(owner, document_id):
            raise AssertionError(f"Failed to publish {item['key']}")
        document_ids[item["key"]] = document_id
    return document_ids


def evaluate(dsn: str, *, suite: str = "independent-v1") -> dict:
    document_path, case_path = SUITES[suite]
    document_rows = json.loads(document_path.read_text(encoding="utf-8"))
    cases = json.loads(case_path.read_text(encoding="utf-8"))
    identities = subjects()
    model = SentenceTransformer("BAAI/bge-small-zh", revision=MODEL_REVISION,
                                local_files_only=True)
    repository = DocumentRepository(":memory:", record_index_events=True)
    pg = PgHybridSearch(dsn)
    try:
        document_ids = load_documents(repository, document_rows, identities)
        pg.initialize()
        pg.bind_source(repository.index_source_id, MODEL_REVISION, rebuild=True)
        bootstrap = drain_index_outbox(repository, pg, model, max_events=100)
        expected_events = sum(1 + bool(item["published"]) for item in document_rows)
        if bootstrap["failed"] or bootstrap["processed"] != expected_events:
            raise AssertionError(f"Shadow indexing failed: {bootstrap}")
        start = time.perf_counter()
        vectors = model.encode([case["question"] for case in cases],
                               normalize_embeddings=True, show_progress_bar=False)
        embedding_ms = round((time.perf_counter() - start) * 1000, 2)
        results = []
        for case, vector in zip(cases, vectors, strict=True):
            subject = identities[case["role"]]
            expected = document_ids.get(case["expected_key"])
            started = time.perf_counter()
            baseline = repository.search_visible(subject, case["question"],
                                                 area=case.get("area"), limit=3)
            baseline_ms = round((time.perf_counter() - started) * 1000, 3)
            started = time.perf_counter()
            candidates = pg.search(subject, case["question"], vector,
                                   area=case.get("area"), limit=3)
            retrieval_ms = round((time.perf_counter() - started) * 1000, 3)
            visible = {record.document_id for record in repository.list_visible(subject)}
            unauthorized = [item["document_id"] for item in candidates
                            if item["document_id"] not in visible]
            gated = resolve_gated_candidates(repository, subject, candidates,
                                             max_vector_distance=FROZEN_DISTANCE,
                                             area=case.get("area"), limit=1)
            hit = gated[0] if gated else None
            quote = None
            semantic_quote = None
            if hit:
                evidence = Evidence(document_id=hit.document.document_id,
                                    version_id=hit.document.version,
                                    location=hit.location, text=hit.text)
                quote = grounded_extract(case["question"], evidence, hit.document.title)
                if quote is None:
                    semantic_quote = semantic_source_quote(model, vector, hit.text)
            selected_quote = quote or semantic_quote
            coverage = evidence_coverage(case["question"], selected_quote)
            evidence_rejected = coverage in {"no_quote", "missing_comparison_entities", "missing_quantity"}
            structured = structured_evidence_check(case["question"], selected_quote)
            structured_rejected = structured not in {"shape_match", "unchecked"}
            evidence_key = None if evidence_rejected else next(
                (key for key, document_id in document_ids.items()
                 if hit and hit.document.document_id == document_id), None)
            results.append({
                "case": case["name"], "role": case["role"], "expected_key": case["expected_key"],
                "baseline_recall_at_3": expected in {item.document.document_id for item in baseline}
                                        if expected else None,
                "hybrid_recall_at_3": expected in {item["document_id"] for item in candidates}
                                      if expected else None,
                "gated_key": next((key for key, document_id in document_ids.items()
                                   if hit and hit.document.document_id == document_id), None),
                "gated_correct": (hit.document.document_id if hit else None) == expected,
                "evidence_coverage": coverage,
                "evidence_key": evidence_key,
                "evidence_correct": (hit.document.document_id if hit and not evidence_rejected else None) == expected,
                "structured_check": structured,
                "structured_correct": (hit.document.document_id if hit and not structured_rejected else None) == expected,
                "top_distance": round(float(candidates[0]["vector_distance"]), 4)
                                if candidates else None,
                "quote": selected_quote,
                "quote_is_source_substring": selected_quote in hit.text if hit and selected_quote else
                                             selected_quote is None,
                "expected_fact_supported": case["expected_fact"] in selected_quote
                                           if expected and selected_quote else False,
                "unauthorized_candidates": unauthorized,
                "baseline_ms": baseline_ms, "hybrid_ms": retrieval_ms,
            })
        positives = [result for result in results if result["expected_key"]]
        negatives = [result for result in results if not result["expected_key"]]
        unauthorized_count = sum(len(result["unauthorized_candidates"]) for result in results)
        if unauthorized_count:
            raise AssertionError("Hybrid SQL returned unauthorized candidates")
        return {
            "dataset": suite, "documents": len(document_rows), "cases": len(cases),
            "document_sha256": hashlib.sha256(document_path.read_bytes()).hexdigest(),
            "case_sha256": hashlib.sha256(case_path.read_bytes()).hexdigest(),
            "model_revision": MODEL_REVISION, "frozen_gate": FROZEN_DISTANCE,
            "positive_cases": len(positives), "negative_cases": len(negatives),
            "baseline_recall_at_3": sum(item["baseline_recall_at_3"] for item in positives),
            "hybrid_recall_at_3": sum(item["hybrid_recall_at_3"] for item in positives),
            "gated_positive_correct": sum(item["gated_correct"] for item in positives),
            "negative_rejections": sum(item["gated_correct"] for item in negatives),
            "surface_positive_retained": sum(item["evidence_correct"] for item in positives),
            "surface_negative_rejections": sum(item["evidence_correct"] for item in negatives),
            "surface_shape_checked": sum(item["evidence_coverage"] in {
                "surface_covered", "missing_comparison_entities", "missing_quantity"}
                for item in results),
            "surface_unchecked": sum(item["evidence_coverage"] == "unchecked" for item in results),
            "structured_positive_retained": sum(item["structured_correct"] for item in positives),
            "structured_negative_rejections": sum(item["structured_correct"] for item in negatives),
            "structured_shape_checked": sum(item["structured_check"] not in {"unchecked", "no_quote"}
                                            for item in results),
            "structured_unchecked": sum(item["structured_check"] == "unchecked" for item in results),
            "fact_supported_quotes": sum(item["expected_fact_supported"] for item in positives),
            "source_substring_checks": sum(item["quote_is_source_substring"] for item in results),
            "unauthorized_candidates": unauthorized_count,
            "embedding_batch_ms": embedding_ms, "bootstrap": bootstrap,
            "failures": [item["case"] for item in results if not item["gated_correct"] or
                         (item["expected_key"] and not item["expected_fact_supported"])],
            "surface_failures": [item["case"] for item in results if not item["evidence_correct"]],
            "structured_failures": [item["case"] for item in results if not item["structured_correct"]],
            "results": results,
            "limitations": "Synthetic documents and exact fact labels. The distance gate and checks were built on earlier suites; each suite becomes development data once inspected. Surface and structured checks are necessary shape tests, not entailment judgments. Not generated-answer accuracy or production latency.",
        }
    finally:
        pg.close()
        repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.getenv("RETRIEVAL_DATABASE_URL",
                        "postgresql://agent_retrieval:local-retrieval-only@127.0.0.1:55432/agent_retrieval"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--suite", choices=sorted(SUITES), default="independent-v1")
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("output already exists; choose a new path to preserve the original run")
    report = evaluate(args.dsn, suite=args.suite)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
