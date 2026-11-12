import json
import unittest

from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.conditional_answers import ConditionDecision, ConditionEvidence, validate_decision
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.security.policy import Classification, Subject, UserRole, Visibility


class ConditionalAnswerTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        auth = LocalAuth([LocalAccount("employee", hash_password("password"), Subject(
            "employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL))])
        self.client = TestClient(create_app(auth, self.documents))
        token = self.client.post("/api/v1/auth/login", json={
            "username": "employee", "password": "password"}).json()["access_token"]
        self.headers = {"Authorization": f"Bearer {token}"}

    def tearDown(self):
        self.documents.close()

    def ask(self, question, conversation_id=None, stream=False):
        response = self.client.post("/api/v1/conversations/answers" + ("/stream" if stream else ""),
                                    headers=self.headers, json={
                                        "question": question, "mode": "extract",
                                        "conversation_id": conversation_id})
        self.assertEqual(response.status_code, 200, response.text)
        if stream:
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ")]
            self.assertEqual(events[-1]["type"], "done")
            return events[-1]
        return response.json()

    def test_known_years_answer_and_missing_years_clarify_in_both_paths(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                known = self.ask("我已经在这个公司工作了7年了，我能放多少天年假？", stream=stream)
                self.assertEqual(known["answer"]["status"], "answered")
                self.assertIn("10 天年假", known["answer"]["paragraphs"][0])
                self.assertEqual(known["answer"]["citations"][0]["document_id"], "2")
                missing = self.ask("我能放多少天年假？", stream=stream)
                self.assertEqual(missing["answer"]["status"], "needs_clarification")
                self.assertEqual(missing["answer"]["clarification"]["field"], "工作年限")
                self.assertEqual(missing["answer"]["citations"][0]["document_id"], "2")
                followup = self.ask("我能放多少天年假？补充条件：我的工作年限是3年。",
                                    missing["conversation_id"], stream=stream)
                self.assertEqual(followup["conversation_id"], missing["conversation_id"])
                self.assertEqual(followup["answer"]["status"], "answered")
                self.assertIn("5 天年假", followup["answer"]["paragraphs"][0])
                history = self.client.get(f"/api/v1/conversations/{missing['conversation_id']}",
                                          headers=self.headers).json()
                self.assertEqual(history["turns"][0]["answer"]["clarification"]["field"], "工作年限")

    def test_revoked_policy_cannot_generate_clarification_or_answer(self):
        self.documents.set_denied("demo-org", "2", "employee-001", True)
        result = self.ask("我能放多少天年假？")
        self.assertEqual(result["answer"]["status"], "no_evidence")
        self.assertIsNone(result["answer"]["clarification"])

    def test_model_numeric_claim_requires_evidence_or_user_input(self):
        evidence = [ConditionEvidence("年假政策", "工作满 5 年每年享受 10 天年假。")]
        self.assertIsNone(validate_decision(ConditionDecision("answer", "可享受 15 天年假。",
                                                              "工作满 5 年每年享受 10 天年假。", 0),
                                            evidence, "我工作了7年"))
        self.assertIsNotNone(validate_decision(ConditionDecision("answer", "可享受 10 天年假。",
                                                                 "工作满 5 年每年享受 10 天年假。", 0),
                                               evidence, "我工作了7年"))
        grade = [ConditionEvidence("体检制度", "职级 P5 享受 2 次体检；职级 P6 享受 3 次体检。")]
        self.assertIsNone(validate_decision(ConditionDecision("answer", "可享受 3 次体检。",
                                                              "职级 P6 享受 3 次体检", 0),
                                            grade, "我的职级是P5，我能体检几次？"))
        self.assertIsNone(validate_decision(ConditionDecision("answer", "可享受 3 次体检。",
                                                              grade[0].text, 0),
                                            grade, "我的职级是P5，我能体检几次？"))

    def test_other_policy_condition_uses_same_clarification_workflow(self):
        policy = "不同职级分别享受体检次数：职级 P5 每年可享受 2 次体检；职级 P6 每年可享受 3 次体检。"
        manager = Subject("manager-001", "demo-org", frozenset({"product"}),
                          Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)
        document_id = self.documents.create_upload(
            manager, title="员工体检制度", filename="health.txt", file_type="TXT", area="制度规范",
            visibility=Visibility.ORGANIZATION, content=policy.encode(), extracted_text=policy)
        self.assertTrue(self.documents.publish(manager, document_id))

        class Resolver:
            calls = 0

            def resolve(self, question, evidence):
                self.calls += 1
                if "P5" in question:
                    return ConditionDecision("answer", "你每年可享受 2 次体检。",
                                             "职级 P5 每年可享受 2 次体检", 0)
                return ConditionDecision("clarify", field="职级", prompt="你的职级是什么？",
                                         input_type="select", options=("P5", "P6"),
                                         quote="职级 P5 每年可享受 2 次体检", evidence_index=0)

        resolver = Resolver()
        auth = LocalAuth([LocalAccount("employee", hash_password("password"), Subject(
            "employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL))])
        client = TestClient(create_app(auth, self.documents, condition_resolver=resolver))
        token = client.post("/api/v1/auth/login", json={
            "username": "employee", "password": "password"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        def ask(question, conversation_id=None):
            response = client.post("/api/v1/conversations/answers", headers=headers, json={
                "question": question, "mode": "extract", "conversation_id": conversation_id})
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        first = ask("我每年能享受多少次体检？")
        self.assertEqual(first["answer"]["status"], "needs_clarification")
        self.assertEqual(first["answer"]["clarification"]["options"], ["P5", "P6"])
        second = ask("我每年能享受多少次体检？补充条件：我的职级是P5。", first["conversation_id"])
        self.assertEqual(second["conversation_id"], first["conversation_id"])
        self.assertEqual(second["answer"]["status"], "answered")
        self.assertIn("2 次体检", second["answer"]["paragraphs"][0])
        self.assertEqual(resolver.calls, 2)


if __name__ == "__main__":
    unittest.main()
