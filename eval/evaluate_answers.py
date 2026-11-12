"""Offline answer and citation evaluation through the authenticated API."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.main import create_app
from eval.evaluate_authorized_search import CASES, SUBJECTS


def evaluate() -> dict:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    documents = DocumentRepository(":memory:", seed_demo=True)
    accounts = LocalAuth([
        LocalAccount(name, hash_password("offline-test-password"), subject)
        for name, subject in SUBJECTS.items()
    ])
    results = []
    try:
        with TestClient(create_app(accounts, documents)) as client:
            headers = {}
            for name in SUBJECTS:
                response = client.post("/api/v1/auth/login", json={
                    "username": name, "password": "offline-test-password",
                })
                response.raise_for_status()
                headers[name] = {"Authorization": f"Bearer {response.json()['access_token']}"}

            for case in cases:
                response = client.post("/api/v1/answers/workflow", headers=headers[case["role"]],
                                       json={"question": case["question"], "area": case.get("area")})
                response.raise_for_status()
                answer = response.json()
                expected_id = case["expected_id"]
                citations = answer["citations"]
                pairs = list(zip(answer["paragraphs"], citations, strict=False))
                hits = documents.search_visible(SUBJECTS[case["role"]], case["question"],
                                                area=case.get("area"), limit=6)
                supported = None if expected_id is None else (
                    len(pairs) == len(answer["paragraphs"]) == len(citations)
                    and bool(citations)
                    and all(
                        any(hit.document.document_id == citation["document_id"]
                            and hit.document.version == citation["version"]
                            and hit.location == citation["location"]
                            and all(part in hit.text for part in paragraph.split(" "))
                            for hit in hits)
                        for paragraph, citation in pairs
                    )
                )
                if expected_id is None:
                    passed = (answer["status"] == "no_evidence" and not citations
                              and len(answer["paragraphs"]) == 1
                              and "无法回答" in answer["paragraphs"][0])
                else:
                    passed = (answer["status"] == "evidence_ready" and supported
                              and expected_id in {item["document_id"] for item in citations}
                              and case["expected_answer"] in " ".join(answer["paragraphs"]))
                results.append({"case": case["name"], "status": answer["status"],
                                "citation_ids": [item["document_id"] for item in citations],
                                "supported": supported, "passed": passed})

            admin_response = client.put("/api/v1/admin/documents/5/deny", headers=headers["admin"],
                                         json={"user_id": SUBJECTS["manager"].user_id, "denied": True})
            admin_response.raise_for_status()
            revoked = client.post("/api/v1/answers/workflow", headers=headers["manager"],
                                  json={"question": "产品发布前有哪些检查项？"})
            revoked.raise_for_status()
            revoked_answer = revoked.json()
            revoked_passed = (revoked_answer["status"] == "no_evidence"
                              and not revoked_answer["citations"]
                              and len(revoked_answer["paragraphs"]) == 1
                              and "无法回答" in revoked_answer["paragraphs"][0])
            results.append({"case": "manager_revoked_release", "status": revoked_answer["status"],
                            "citation_ids": [item["document_id"] for item in revoked_answer["citations"]],
                            "supported": None, "passed": revoked_passed})
    finally:
        documents.close()
    answer_cases = [item for item in results if item["supported"] is not None]
    refusal_cases = [item for item in results if item["supported"] is None]
    return {"dataset": Path(CASES).name, "dataset_version": "m3-synthetic-v1",
            "mode": "deterministic_extract", "cases": len(results),
            "passed": sum(item["passed"] for item in results),
            "citation_supported": sum(item["supported"] is True for item in answer_cases),
            "citation_total": len(answer_cases),
            "refusals_passed": sum(item["passed"] for item in refusal_cases),
            "refusal_total": len(refusal_cases), "results": results}


if __name__ == "__main__":
    report = evaluate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] == report["cases"] else 1)
