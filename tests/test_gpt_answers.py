"""Offline coverage for the optional, evidence-constrained GPT quote endpoint."""

import unittest

from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.gpt_answers import OpenAIQuoteSelector, QuoteChoice, QuoteSelection
from app.api.main import create_app
from app.security.policy import Classification, Subject, UserRole


class FakeSelector:
    def __init__(self, callback=None):
        self.callback = callback or (lambda evidence: QuoteSelection(quotes=[
            QuoteChoice(evidence_index=0, quote=evidence[0].text[:20])
        ]))
        self.calls = []

    def select(self, question, evidence):
        self.calls.append((question, evidence))
        return self.callback(evidence)


class GptAnswerTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        accounts = [
            LocalAccount("employee", hash_password("password"), Subject(
                "employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("manager", hash_password("password"), Subject(
                "manager-001", "demo-org", frozenset({"product"}), Classification.CONFIDENTIAL,
                role=UserRole.DEPARTMENT_MANAGER)),
            LocalAccount("admin", hash_password("password"), Subject(
                "admin-001", "demo-org", frozenset({"it"}), Classification.RESTRICTED,
                role=UserRole.SYSTEM_ADMIN)),
        ]
        self.auth = LocalAuth(accounts)
        self.selector = FakeSelector()
        self.client = TestClient(create_app(self.auth, self.documents, quote_selector=self.selector))
        self.question = {"question": "产品发布前有哪些检查项？"}
        self.url = "/api/v1/answers/gpt"

    def tearDown(self):
        self.documents.close()

    def headers(self, username):
        token = self.client.post("/api/v1/auth/login", json={
            "username": username, "password": "password"
        }).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def test_capability_requires_login_and_explicit_configuration(self):
        self.assertEqual(self.client.get("/api/v1/capabilities").status_code, 401)
        enabled = self.client.get("/api/v1/capabilities", headers=self.headers("manager")).json()
        self.assertTrue(enabled["gpt_quotes"])
        self.assertIsInstance(enabled["collaboration"], bool)
        disabled = TestClient(create_app(self.auth, self.documents))
        disabled.app.state.gpt_enabled = False
        headers = self.headers("manager")
        self.assertFalse(disabled.get("/api/v1/capabilities", headers=headers).json()["gpt_quotes"])
        response = disabled.post(self.url, headers=headers, json=self.question)
        self.assertEqual((response.status_code, response.json()["code"]), (503, "GPT_NOT_CONFIGURED"))
        self.assertEqual(self.selector.calls, [])

    def test_authorization_and_missing_evidence_skip_model(self):
        self.assertEqual(self.client.post(self.url, json=self.question).status_code, 401)
        for username in ("employee", "admin"):
            answer = self.client.post(self.url, headers=self.headers(username), json=self.question).json()
            self.assertEqual(answer["status"], "no_evidence")
            self.assertEqual(answer["citations"], [])
        answer = self.client.post(self.url, headers=self.headers("manager"),
                                  json={**self.question, "area": "制度规范"}).json()
        self.assertEqual(answer["status"], "no_evidence")
        self.assertEqual(self.selector.calls, [])

    def test_valid_quote_cites_only_authorized_source(self):
        answer = self.client.post(self.url, headers=self.headers("manager"), json=self.question)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["status"], "evidence_ready")
        self.assertEqual(answer.json()["citations"][0]["document_id"], "5")
        self.assertIn(answer.json()["paragraphs"][0], self.selector.calls[0][1][0].text)
        self.assertEqual({item.document_id for item in self.selector.calls[0][1]}, {"5"})

    def test_invalid_source_and_invented_quote_are_rejected(self):
        for choice in (QuoteChoice(evidence_index=999, quote="虚构的原文引句"),
                       QuoteChoice(evidence_index=0, quote="虚构的原文引句")):
            with self.subTest(choice=choice):
                self.selector.callback = lambda evidence, choice=choice: QuoteSelection(quotes=[choice])
                response = self.client.post(self.url, headers=self.headers("manager"), json=self.question)
                self.assertEqual((response.status_code, response.json()["code"]),
                                 (502, "MODEL_UNSUPPORTED_QUOTE"))

    def test_model_failure_and_empty_selection(self):
        def fail(evidence):
            raise RuntimeError("provider is down")

        self.selector.callback = fail
        response = self.client.post(self.url, headers=self.headers("manager"), json=self.question)
        self.assertEqual((response.status_code, response.json()["code"]), (503, "MODEL_UNAVAILABLE"))
        self.selector.callback = lambda evidence: QuoteSelection(quotes=[])
        answer = self.client.post(self.url, headers=self.headers("manager"), json=self.question).json()
        self.assertEqual(answer["status"], "no_evidence")
        self.assertEqual(answer["citations"], [])

    def test_revocation_during_model_call_drops_quote(self):
        def revoke(evidence):
            self.documents.set_denied("demo-org", "5", "manager-001", True)
            return QuoteSelection(quotes=[QuoteChoice(evidence_index=0, quote=evidence[0].text[:20])])

        self.selector.callback = revoke
        answer = self.client.post(self.url, headers=self.headers("manager"), json=self.question).json()
        self.assertEqual(answer["status"], "no_evidence")
        self.assertEqual(answer["citations"], [])

    def test_openai_adapter_requests_structured_non_stored_output(self):
        class FakeResponses:
            def __init__(self):
                self.kwargs = None

            def parse(self, **kwargs):
                self.kwargs = kwargs
                return type("Response", (), {"output_parsed": {"quotes": []}})()

        client = type("Client", (), {"responses": FakeResponses()})()
        evidence = self.documents.search_visible(
            Subject("manager-001", "demo-org", frozenset({"product"}), Classification.CONFIDENTIAL,
                    role=UserRole.DEPARTMENT_MANAGER),
            self.question["question"], limit=1,
        )
        from app.workflow.graph import Evidence
        sources = [Evidence(document_id=hit.document.document_id, version_id=hit.document.version,
                            location=hit.location, text=hit.text) for hit in evidence]
        result = OpenAIQuoteSelector(client, "test-model").select(self.question["question"], sources)
        self.assertEqual(result.quotes, [])
        self.assertEqual(client.responses.kwargs["model"], "test-model")
        self.assertIs(client.responses.kwargs["text_format"], QuoteSelection)
        self.assertIs(client.responses.kwargs["store"], False)
        self.assertIn("sources", client.responses.kwargs["input"][1]["content"])
        client.responses.parse = lambda **kwargs: type("Response", (), {"output_parsed": None})()
        with self.assertRaisesRegex(ValueError, "MODEL_NO_STRUCTURED_OUTPUT"):
            OpenAIQuoteSelector(client, "test-model").select(self.question["question"], sources)


if __name__ == "__main__":
    unittest.main()
