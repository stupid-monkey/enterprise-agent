"""Shadow retrieval cannot bypass the current authoritative source policy."""

import unittest

from app.api.documents import DocumentRepository
from app.api.hybrid_evidence import calibrate_distance_gate, resolve_gated_candidates
from app.security.policy import Classification, Subject, UserRole, Visibility


class HybridEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        self.employee = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
        self.manager = Subject("manager-001", "demo-org", frozenset({"product"}),
                               Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)
        self.row = self.documents._db.execute("""SELECT s.segment_id, s.text, d.document_id,
            d.version FROM document_segments s JOIN documents d USING(document_id)
            WHERE d.document_id = '1'""").fetchone()
        self.candidate = {"segment_id": self.row["segment_id"], "document_id": "1",
                          "version": self.row["version"], "body": self.row["text"],
                          "vector_distance": 0.1}

    def tearDown(self):
        self.documents.close()

    def resolve(self, candidate=None, subject=None):
        return resolve_gated_candidates(self.documents, subject or self.employee,
                                        [candidate or self.candidate], max_vector_distance=0.22)

    def test_valid_candidate_and_distance_gate(self):
        self.assertEqual(self.resolve()[0].document.document_id, "1")
        self.assertFalse(self.resolve({**self.candidate, "vector_distance": 0.3}))
        self.assertFalse(self.resolve({**self.candidate, "vector_distance": float("nan")}))

    def test_revocation_and_cross_user(self):
        self.assertTrue(self.documents.set_denied("demo-org", "1", self.employee.user_id, True))
        self.assertFalse(self.resolve())
        self.assertTrue(self.resolve(subject=self.manager))
        other_org = Subject("employee-001", "other-org", frozenset({"general"}), Classification.INTERNAL)
        self.assertFalse(self.resolve(subject=other_org))

    def test_stale_version_and_tampered_body(self):
        self.assertFalse(self.resolve({**self.candidate, "version": "old"}))
        self.assertFalse(self.resolve({**self.candidate, "body": "fabricated"}))
        self.assertFalse(self.resolve({**self.candidate, "segment_id": [1]}))

    def test_unpublished_and_area_filter(self):
        pending = self.documents.create_upload(self.manager, title="Draft", filename="draft.txt",
            file_type="TXT", area="项目空间", visibility=Visibility.ORGANIZATION,
            content=b"Draft information", extracted_text="Draft information")
        row = self.documents._db.execute("""SELECT s.segment_id, s.text FROM document_segments s
            WHERE s.document_id = ?""", (pending,)).fetchone()
        self.assertFalse(self.resolve({"segment_id": row["segment_id"], "document_id": pending,
                                       "version": "v1.0", "body": row["text"],
                                       "vector_distance": 0.1}))
        self.assertFalse(resolve_gated_candidates(self.documents, self.employee,
            [self.candidate], max_vector_distance=0.22, area="产品资料"))

    def test_calibration_uses_only_given_rows(self):
        rows = [{"expected_id": "1", "hybrid_scores": [{"document_id": "1", "vector_distance": 0.1}]},
                {"expected_id": None, "hybrid_scores": [{"document_id": "2", "vector_distance": 0.3}]}]
        self.assertEqual(calibrate_distance_gate(rows), 0.2)


if __name__ == "__main__":
    unittest.main()
