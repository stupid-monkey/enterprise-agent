"""Keep the independent retrieval report's labels and denominators auditable."""

import json
import hashlib
import unittest

from eval.evaluate_pg_hybrid_independent import FROZEN_DISTANCE, SUITES


FROZEN = {
    "independent-v1": (12, 24, 12),
    "holdout-v2": (11, 20, 10),
    "stress-v3": (12, 21, 10),
    "source-group-v4": (10, 19, 9),
    "source-group-v5": (11, 22, 10),
}
HOLDOUT_HASHES = (
    "ffb41b491767077064703b30f9f7898007b2635ed265b077b3155887807a2182",
    "9f562c25142ccc52715ee8acc653b5ec605379184c2c1d04ff2f623b5fdb3783",
)
STRESS_HASHES = (
    "5c0873fa7b3575bd5f9e9ee44df2a6d45a0ec0f6c9c84b268743ee968c1b1b1c",
    "6fad005e82ffbce9b4a3a0796ff2b6c6d49abed5d62800d689af51906e1886fc",
)
SOURCE_GROUP_HASHES = (
    "34845c922956a221dcbf62564dbe4cc381b10491a49b7587b50e560388ba908e",
    "de0c48f180eaf6d60b96cfae3e59922844e316fcf10d065d57eafd174c398351",
)
SOURCE_GROUP_V5_HASHES = (
    "4687391c88972cc42d0a71b9f0f6ad05a5462594507ddda61199d4f344ed0663",
    "f2891d7722c426b853123124098bda24612b90436d10ce11fc5c8c304b1a88ea",
)


class IndependentRetrievalDatasetTests(unittest.TestCase):
    def test_labels_refer_to_unique_source_facts(self):
        self.assertEqual(FROZEN_DISTANCE, 0.2215)
        for suite, (document_path, case_path) in SUITES.items():
            with self.subTest(suite=suite):
                documents = json.loads(document_path.read_text(encoding="utf-8"))
                cases = json.loads(case_path.read_text(encoding="utf-8"))
                by_key = {row["key"]: row for row in documents}
                self.assertEqual(len(documents), len(by_key))
                self.assertEqual(len(cases), len({row["name"] for row in cases}))
                self.assertEqual((len(documents), len(cases),
                                  sum(row["expected_key"] is not None for row in cases)),
                                 FROZEN[suite])
                for case in cases:
                    key = case["expected_key"]
                    if key is not None:
                        self.assertTrue(by_key[key]["published"], case["name"])
                        self.assertIn(case["expected_fact"], by_key[key]["text"], case["name"])

    def test_holdout_bytes_were_frozen_before_first_run(self):
        document_path, case_path = SUITES["holdout-v2"]
        self.assertEqual((hashlib.sha256(document_path.read_bytes()).hexdigest(),
                          hashlib.sha256(case_path.read_bytes()).hexdigest()), HOLDOUT_HASHES)

    def test_stress_bytes_were_frozen_before_first_run(self):
        document_path, case_path = SUITES["stress-v3"]
        self.assertEqual((hashlib.sha256(document_path.read_bytes()).hexdigest(),
                          hashlib.sha256(case_path.read_bytes()).hexdigest()), STRESS_HASHES)

    def test_source_group_bytes_and_keys_are_distinct(self):
        document_path, case_path = SUITES["source-group-v4"]
        self.assertEqual((hashlib.sha256(document_path.read_bytes()).hexdigest(),
                          hashlib.sha256(case_path.read_bytes()).hexdigest()), SOURCE_GROUP_HASHES)
        new_keys = {row["key"] for row in json.loads(document_path.read_text(encoding="utf-8"))}
        old_keys = {row["key"] for suite, (path, _) in SUITES.items()
                    if suite != "source-group-v4"
                    for row in json.loads(path.read_text(encoding="utf-8"))}
        self.assertFalse(new_keys & old_keys)

    def test_source_group_v5_bytes_and_known_v3_key_overlap(self):
        document_path, case_path = SUITES["source-group-v5"]
        self.assertEqual((hashlib.sha256(document_path.read_bytes()).hexdigest(),
                          hashlib.sha256(case_path.read_bytes()).hexdigest()), SOURCE_GROUP_V5_HASHES)
        new_keys = {row["key"] for row in json.loads(document_path.read_text(encoding="utf-8"))}
        old_keys = {row["key"] for suite, (path, _) in SUITES.items()
                    if suite != "source-group-v5"
                    for row in json.loads(path.read_text(encoding="utf-8"))}
        self.assertEqual(new_keys & old_keys, {"restore_drill"})
        v4_path, _ = SUITES["source-group-v4"]
        self.assertFalse(new_keys & {row["key"] for row in
                                     json.loads(v4_path.read_text(encoding="utf-8"))})


if __name__ == "__main__":
    unittest.main()
