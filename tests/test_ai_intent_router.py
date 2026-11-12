import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.intent import high_confidence_intent, needs_deliverable_classification
from app.api.intent_model import IntentDecision, OpenAIIntentClassifier, focus_supported, retrieval_query
from app.api.main import create_app
from app.security.policy import Classification, Subject, UserRole


class FakeClassifier:
    def __init__(self, result=IntentDecision("knowledge", "产品发布前需要检查哪些事项？", ("产品发布",))):
        self.result = result
        self.calls = []

    def classify(self, question, previous_knowledge):
        self.calls.append((question, previous_knowledge))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeResponder:
    def __init__(self):
        self.calls = []

    def stream(self, question, history, profile):
        self.calls.append(question)
        yield "普通问题回答"


class AIIntentRouterTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        self.manager = Subject("manager-001", "demo-org", frozenset({"product"}),
                               Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)
        self.employee = Subject("employee-001", "demo-org", frozenset({"general"}),
                                Classification.INTERNAL)
        auth = LocalAuth([LocalAccount("manager", hash_password("password"), self.manager),
                          LocalAccount("employee", hash_password("password"), self.employee)])
        self.classifier = FakeClassifier()
        self.responder = FakeResponder()
        self.client = TestClient(create_app(auth, self.documents, general_responder=self.responder,
                                            intent_classifier=self.classifier))

    def tearDown(self):
        self.documents.close()

    def headers(self, username="manager"):
        token = self.client.post("/api/v1/auth/login", json={
            "username": username, "password": "password"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def ask(self, question, *, stream=True, username="manager", conversation_id=None):
        suffix = "/stream" if stream else ""
        response = self.client.post(f"/api/v1/conversations/answers{suffix}",
                                    headers=self.headers(username), json={
                                        "question": question, "mode": "extract",
                                        "conversation_id": conversation_id})
        self.assertEqual(response.status_code, 200)
        if stream:
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ")]
            return events[0], events[-1]
        return None, response.json()

    def test_model_routes_ambiguous_work_question_on_both_endpoints(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                meta, result = self.ask("新品发售前要做什么？", stream=stream)
                self.assertEqual(self.classifier.calls[-1], ("新品发售前要做什么？", False))
                if stream:
                    self.assertEqual(meta["intent"], "knowledge")
                    self.assertEqual(result["type"], "done")
                self.assertNotEqual(result["answer"]["status"], "general")
                self.assertEqual(result["answer"]["citations"][0]["document_id"], "5")
                self.assertEqual(self.responder.calls, [])

    def test_model_routes_general_question_on_both_endpoints(self):
        self.classifier.result = IntentDecision("general")
        for stream in (True, False):
            with self.subTest(stream=stream):
                meta, result = self.ask("Python 怎么写文件？", stream=stream)
                if stream:
                    self.assertEqual(meta["intent"], "general")
                self.assertEqual(result["answer"]["status"], "general")
        self.assertEqual(self.responder.calls, ["Python 怎么写文件？"] * 2)

    def test_self_identity_uses_authenticated_role_even_if_model_would_choose_knowledge(self):
        self.client.app.state.memories.upsert(self.manager, "role", "算法工程师")
        self.client.app.state.general_enabled = False
        for stream in (True, False):
            with self.subTest(stream=stream):
                meta, result = self.ask("我是什么身份", stream=stream)
                if stream:
                    self.assertEqual(meta["intent"], "general")
                self.assertEqual(result["answer"]["status"], "general")
                answer = " ".join(result["answer"]["paragraphs"])
                self.assertIn("部门负责人", answer)
                self.assertIn("算法工程师", answer)
                self.assertEqual(result["answer"]["citations"], [])
        _, employee = self.ask("我的身份是什么？", username="employee")
        self.assertIn("普通员工", " ".join(employee["answer"]["paragraphs"]))
        self.assertNotIn("算法工程师", " ".join(employee["answer"]["paragraphs"]))
        self.assertEqual(self.classifier.calls, [])
        self.assertEqual(self.responder.calls, [])

    def test_account_overview_uses_current_subject_and_document_acl(self):
        self.client.app.state.general_enabled = False
        with mock.patch.object(self.client.app.state.memories, "enqueue_suggestion") as enqueue:
            for username, subject, question, expected in (
                    ("manager", self.manager, "我属于哪个部门？", "product"),
                    ("employee", self.employee, "我的账号是什么？", "employee-001"),
                    ("employee", self.employee, "我在公司的资料权限有哪些？", "普通员工"),
                    ("manager", self.manager, "我能访问哪些资料？", "部门负责人")):
                with self.subTest(question=question):
                    for stream in (True, False):
                        meta, result = self.ask(question, stream=stream, username=username)
                        if stream:
                            self.assertEqual(meta["intent"], "general")
                        self.assertEqual(result["answer"]["status"], "general")
                        self.assertEqual(result["answer"]["citations"], [])
                        answer = " ".join(result["answer"]["paragraphs"])
                        self.assertIn(expected, answer)
                        if "权限" in question or "资料" in question:
                            visible = self.documents.list_visible(subject)
                            self.assertIn(f"**{len(visible)}** 份", answer)
                            for item in visible[:8]:
                                self.assertIn(item.title, answer)
            enqueue.assert_not_called()
        self.assertEqual(self.classifier.calls, [])
        self.assertEqual(self.responder.calls, [])

    def test_clear_intent_skips_model_and_followup_context_is_sent(self):
        _, first = self.ask("我们公司的产品发布前有哪些检查项？")
        _, greeting = self.ask("你好")
        self.assertEqual(self.classifier.calls, [])
        self.assertEqual(greeting["answer"]["status"], "general")
        self.classifier.result = IntentDecision("general")
        self.ask("Python 怎么写文件？", conversation_id=first["conversation_id"])
        self.assertEqual(self.classifier.calls[-1], ("Python 怎么写文件？", True))

    def test_public_definition_and_writing_requests_reach_model(self):
        self.classifier.result = IntentDecision("general")
        for question in ("产品发布的概念是什么？", "帮我写一段新品发布会主持词",
                         "中国法律规定的年假最低有多少天？", "报销的会计定义是什么？"):
            with self.subTest(question=question):
                meta, result = self.ask(question)
                self.assertEqual(meta["intent"], "general")
                self.assertEqual(result["answer"]["status"], "general")
        self.assertEqual(len(self.classifier.calls), 4)

    def test_high_confidence_only_uses_explicit_context(self):
        self.assertEqual(high_confidence_intent("你好"), "general")
        self.assertEqual(high_confidence_intent("我们公司的发布审批由谁完成？"), "knowledge")
        self.assertIsNone(high_confidence_intent("产品发布的概念是什么？"))
        self.assertIsNone(high_confidence_intent("年假按工龄怎么计算？"))
        self.assertTrue(needs_deliverable_classification("请根据我们公司年假资料准备给领导看的内容"))
        self.assertTrue(needs_deliverable_classification("把制度变成一页可以直接发的说明"))
        self.assertTrue(needs_deliverable_classification("请根据资料准备给新员工阅读的内容"))
        self.assertTrue(needs_deliverable_classification("给明天的会议备一段文字"))
        self.assertFalse(needs_deliverable_classification("我们公司年假有多少天？"))
        self.assertTrue(focus_supported(("发布", "上线"), "产品发布检查清单", "安全测试与回滚方案"))
        self.assertTrue(focus_supported(("年假", "休假"), "员工年假办法", "工作满一年五天"))
        self.assertTrue(focus_supported(("差旅审批", "差旅单"), "差旅报销管理制度",
                                        "经部门负责人审批", "差旅单审批及复核顺序"))
        self.assertTrue(focus_supported(("版本投产", "上线检查"), "产品发布检查清单",
                                        "安全测试与回滚方案", "新版本投产前检查项、产品上线检查清单"))
        self.assertFalse(focus_supported(("住房补贴", "住房津贴"), "住房公积金",
                                         "员工申请流程", "公司住房补贴员工申请资格"))
        self.assertEqual(retrieval_query(IntentDecision("knowledge", "年假按工龄计算规则", ("年假", "休假")),
                                         "年假按工龄怎么计算？"), "年假按工龄计算规则 休假")

    def test_model_failure_or_invalid_output_falls_back(self):
        for result in (TimeoutError("secret diagnostic"), "unknown"):
            with self.subTest(result=result):
                self.classifier.result = result
                meta, answer = self.ask("Python 怎么写文件？")
                self.assertEqual(meta["intent"], "general")
                self.assertEqual(answer["answer"]["status"], "general")

    def test_implicit_document_goal_survives_stream_and_history(self):
        self.classifier.result = IntentDecision("knowledge", "员工年假休假管理办法", ("年假",), True)
        saved = []
        for stream in (True, False):
            with self.subTest(stream=stream):
                meta, result = self.ask("请根据我们公司年假资料准备给领导看的内容", stream=stream)
                self.assertEqual(result["answer"]["status"], "evidence_ready")
                self.assertTrue(result["answer"]["document_requested"])
                self.assertTrue(result["answer"]["citations"])
                history = self.client.get(
                    f"/api/v1/conversations/{result['conversation_id']}",
                    headers=self.headers()).json()
                self.assertTrue(history["turns"][0]["answer"]["document_requested"])
                saved.append(result)
        self.assertEqual(len(self.classifier.calls), 2)
        for document_id in {citation["document_id"] for result in saved
                            for citation in result["answer"]["citations"]}:
            self.documents.set_denied("demo-org", document_id, self.manager.user_id, True)
        for result in saved:
            history = self.client.get(
                f"/api/v1/conversations/{result['conversation_id']}",
                headers=self.headers()).json()
            self.assertEqual(history["turns"][0]["answer"]["status"], "source_unavailable")
            self.assertFalse(history["turns"][0]["answer"]["document_requested"])

    def test_document_goal_without_evidence_is_not_offered(self):
        self.classifier.result = IntentDecision("knowledge", "公司住房补贴政策", ("住房补贴",), True)
        _, result = self.ask("帮我整理住房补贴内容给领导看", username="employee")
        self.assertEqual(result["answer"]["status"], "no_evidence")
        self.assertFalse(result["answer"]["document_requested"])

    def test_model_route_does_not_bypass_document_acl(self):
        meta, result = self.ask("新品发售前要做什么？", username="employee")
        self.assertEqual(meta["intent"], "knowledge")
        self.assertEqual(result["answer"]["citations"], [])

    def test_rewrite_cannot_cite_unrelated_document(self):
        self.classifier.result = IntentDecision(
            "knowledge", "公司住房补贴、住房津贴或住房补助政策及员工申请资格", ("住房补贴", "住房津贴"))
        self.client.app.state.auto_react_enabled = True

        class UnexpectedReact:
            def complete(self, *args, **kwargs):
                raise AssertionError("AI rewritten retrieval should not invoke ReAct")

        self.client.app.state.react_model = UnexpectedReact()
        for stream in (True, False):
            with self.subTest(stream=stream):
                meta, result = self.ask("公司有没有住房补贴？", stream=stream)
                if stream:
                    self.assertEqual(meta["intent"], "knowledge")
                self.assertEqual(result["answer"]["status"], "no_evidence")
                self.assertEqual(result["answer"]["citations"], [])


class IntentModelContractTests(unittest.TestCase):
    def test_only_question_and_context_are_sent(self):
        class Responses:
            output_text = '{"intent":"knowledge","search_query":"产品发布前需要检查什么","focus_terms":["产品发布"],"document_requested":false}'

            def create(self, **kwargs):
                self.request = kwargs
                return type("Result", (), {"output_text": self.output_text})()

        model = OpenAIIntentClassifier.__new__(OpenAIIntentClassifier)
        model.client = type("Client", (), {"responses": Responses()})()
        model.model = "test-model"
        model.api_style = "responses"
        self.assertEqual(model.classify("新品发售前要做什么？", False),
                         IntentDecision("knowledge", "产品发布前需要检查什么", ("产品发布",)))
        payload = model.client.responses.request
        self.assertFalse(payload["store"])
        self.assertEqual(json.loads(payload["input"][1]["content"]), {
            "question": "新品发售前要做什么？", "previous_knowledge": False})
        self.assertNotIn("document", payload["input"][1]["content"])
        model.client.responses.output_text = (
            '{"intent":"knowledge","search_query":"员工年假办法",'
            '"focus_terms":["年假"],"document_requested":true}')
        self.assertTrue(model.classify("准备给领导看的年假内容", False).document_requested)
        model.client.responses.output_text = (
            '{"intent":"general","search_query":null,'
            '"focus_terms":[],"document_requested":true}')
        with self.assertRaisesRegex(ValueError, "INTENT_OUTPUT_INVALID"):
            model.classify("写一份普通模板", False)
