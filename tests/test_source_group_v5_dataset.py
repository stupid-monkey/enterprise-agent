import json
import unittest
from pathlib import Path

from app.api.documents import DocumentRepository
from eval.evaluate_pg_hybrid_independent import load_documents, subjects


ROOT = Path("eval")


class SourceGroupV5DatasetTests(unittest.TestCase):
    def test_new_source_keys_and_labeled_positive_visibility(self):
        documents = json.loads((ROOT / "hybrid_source_group_v5_documents.json")
                               .read_text(encoding="utf-8"))
        cases = json.loads((ROOT / "hybrid_source_group_v5_cases.json")
                           .read_text(encoding="utf-8"))
        previous = json.loads((ROOT / "hybrid_source_group_v4_documents.json")
                              .read_text(encoding="utf-8"))
        keys = {row["key"] for row in documents}
        self.assertEqual((len(documents), len(keys), len(cases)), (11, 11, 22))
        self.assertFalse(keys & {row["key"] for row in previous})
        self.assertEqual(len({case["name"] for case in cases}), len(cases))
        positives = [case for case in cases if case["expected_key"]]
        self.assertEqual(len(positives), 10)
        by_key = {row["key"]: row for row in documents}
        self.assertTrue(all(case["expected_key"] in keys and
                            case["expected_fact"] in by_key[case["expected_key"]]["text"]
                            for case in positives))
        identities = subjects()
        repository = DocumentRepository(":memory:")
        try:
            ids = load_documents(repository, documents, identities)
            for case in positives:
                visible = {record.document_id for record in
                           repository.list_visible(identities[case["role"]])}
                self.assertIn(ids[case["expected_key"]], visible, case["name"])
            for case in cases:
                if case["name"] in {"finance_denied", "secret_denied",
                                    "other_org_denied", "draft_meal_denied"}:
                    forbidden = {"finance_denied": "invoice_intake",
                                 "secret_denied": "secret_license",
                                 "other_org_denied": "other_vehicle",
                                 "draft_meal_denied": "draft_meal"}[case["name"]]
                    visible = {record.document_id for record in
                               repository.list_visible(identities[case["role"]])}
                    self.assertNotIn(ids[forbidden], visible, case["name"])
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
