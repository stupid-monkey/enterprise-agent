"""Deterministic retrieval checks using only synthetic documents and no model calls."""

import json
from pathlib import Path

from app.api.documents import DocumentRepository
from app.security.policy import Classification, Subject, UserRole


CASES = Path(__file__).with_name("authorized_search_cases.json")
SUBJECTS = {
    "employee": Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL),
    "manager": Subject("manager-001", "demo-org", frozenset({"product"}),
                       Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER),
    "admin": Subject("admin-001", "demo-org", frozenset({"it"}),
                     Classification.RESTRICTED, role=UserRole.SYSTEM_ADMIN),
}


def evaluate() -> dict:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    repo = DocumentRepository(":memory:", seed_demo=True)
    results = []
    try:
        for case in cases:
            subject = SUBJECTS[case["role"]]
            visible = {record.document_id for record in repo.list_visible(subject)}
            hits = repo.search_visible(subject, case["question"], area=case.get("area"), limit=3)
            ids = [hit.document.document_id for hit in hits]
            expected = case["expected_id"]
            passed = (expected in ids if expected else not ids) and all(item in visible for item in ids)
            results.append({"case": case["name"], "expected_id": expected,
                            "retrieved_ids": ids, "passed": passed})
    finally:
        repo.close()
    return {"dataset": CASES.name, "cases": len(results),
            "passed": sum(item["passed"] for item in results), "results": results}


if __name__ == "__main__":
    report = evaluate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] == report["cases"] else 1)
