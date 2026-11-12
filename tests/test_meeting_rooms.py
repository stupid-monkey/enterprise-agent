import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.main import create_app
from app.api.meeting_rooms import MeetingRoomRepository
from app.security.policy import Classification, Subject, UserRole


class MeetingRoomTests(unittest.TestCase):
    def setUp(self):
        self.accounts = LocalAuth([
            LocalAccount("manager", hash_password("password"), Subject(
                "manager-1", "org-a", frozenset({"product"}), Classification.CONFIDENTIAL,
                role=UserRole.DEPARTMENT_MANAGER)),
            LocalAccount("employee", hash_password("password"), Subject(
                "employee-1", "org-a", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("outsider", hash_password("password"), Subject(
                "outsider-1", "org-a", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("other-org", hash_password("password"), Subject(
                "other-1", "org-b", frozenset({"product"}), Classification.INTERNAL)),
        ])
        self.repo = MeetingRoomRepository(":memory:")
        class FakeMinutes:
            def __init__(self):
                self.received = []

            def select(self, messages):
                self.received = messages
                return {"discussion": [{"message_id": messages[0]["id"], "quote": messages[0]["text"]}],
                        "decisions": [], "actions": []}

        self.minutes_model = FakeMinutes()
        self.client = TestClient(create_app(self.accounts, meeting_rooms=self.repo,
                                             minutes_model=self.minutes_model))
        self.headers = {name: self.login(name) for name in
                        ("manager", "employee", "outsider", "other-org")}

    def tearDown(self):
        self.repo.close()

    def login(self, name):
        token = self.client.post("/api/v1/auth/login", json={
            "username": name, "password": "password"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def create(self):
        response = self.client.post("/api/v1/meeting-rooms", headers=self.headers["manager"],
                                    json={"department_id": "product", "title": "产品部会议室"})
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def test_invite_chat_and_remove(self):
        room_id = self.create()
        path = f"/api/v1/meeting-rooms/{room_id}"
        for name in ("employee", "outsider", "other-org"):
            self.assertEqual(self.client.get(path, headers=self.headers[name]).status_code, 404)
            self.assertEqual(self.client.get(path + "/messages", headers=self.headers[name]).status_code, 404)
            self.assertEqual(self.client.post(path + "/messages", headers=self.headers[name],
                                              json={"text": "hello"}).status_code, 404)
        invited = self.client.post(path + "/members", headers=self.headers["manager"],
                                   json={"user_id": "employee-1"})
        self.assertEqual(invited.status_code, 200)
        self.assertEqual(len(invited.json()["members"]), 2)
        self.assertEqual(len(self.client.get("/api/v1/meeting-rooms",
                                             headers=self.headers["employee"]).json()), 1)
        sent = self.client.post(path + "/messages", headers=self.headers["employee"],
                                json={"text": "  今天讨论发布计划  "})
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(sent.json()["text"], "今天讨论发布计划")
        self.assertEqual(self.client.get(path + "/messages", headers=self.headers["manager"]).json()[0]["text"],
                         "今天讨论发布计划")
        self.assertEqual(self.client.post(path + "/members", headers=self.headers["employee"],
                                          json={"user_id": "outsider-1"}).status_code, 403)
        self.assertEqual(self.client.delete(path + "/members/employee-1",
                                            headers=self.headers["manager"]).status_code, 204)
        self.assertEqual(self.client.get(path + "/messages", headers=self.headers["employee"]).status_code, 404)

    def test_creation_and_directory_boundaries(self):
        self.assertEqual(self.client.get("/api/v1/meeting-rooms/candidates",
                                         headers=self.headers["employee"]).status_code, 403)
        candidates = self.client.get("/api/v1/meeting-rooms/candidates",
                                     headers=self.headers["manager"]).json()
        self.assertEqual({item["user_id"] for item in candidates},
                         {"manager-1", "employee-1", "outsider-1"})
        self.assertEqual(self.client.post("/api/v1/meeting-rooms", headers=self.headers["employee"],
                                          json={"department_id": "general", "title": "普通室"}).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/meeting-rooms", headers=self.headers["manager"],
                                          json={"department_id": "general", "title": "越权室"}).status_code, 403)
        room_id = self.create()
        self.assertEqual(self.client.post("/api/v1/meeting-rooms", headers=self.headers["manager"],
                                          json={"department_id": "product", "title": "重复室"}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/v1/meeting-rooms/{room_id}/members",
                                          headers=self.headers["manager"],
                                          json={"user_id": "other-1"}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/v1/meeting-rooms/{room_id}/members/manager-1",
                                            headers=self.headers["manager"]).status_code, 404)

    def test_message_validation_and_history_cursor(self):
        room_id = self.create()
        path = f"/api/v1/meeting-rooms/{room_id}/messages"
        self.assertEqual(self.client.post(path, headers=self.headers["manager"],
                                          json={"text": "   "}).status_code, 422)
        first = self.client.post(path, headers=self.headers["manager"], json={"text": "第一条"}).json()
        second = self.client.post(path, headers=self.headers["manager"], json={"text": "第二条"}).json()
        self.assertEqual([item["id"] for item in self.client.get(path + f"?after_id={first['id']}",
                          headers=self.headers["manager"]).json()], [second["id"]])
        self.assertEqual([item["id"] for item in self.client.get(path + f"?before_id={second['id']}",
                          headers=self.headers["manager"]).json()], [first["id"]])

    def test_minutes_are_bounded_cited_and_members_only(self):
        room_id = self.create()
        path = f"/api/v1/meeting-rooms/{room_id}"
        self.assertEqual(self.client.post(path + "/minutes", headers=self.headers["manager"]).status_code, 409)
        self.client.post(path + "/members", headers=self.headers["manager"],
                         json={"user_id": "employee-1"})
        for number in range(65):
            self.client.post(path + "/messages", headers=self.headers["manager"],
                             json={"text": f"议题 {number}"})
        self.assertEqual(self.client.post(path + "/minutes", headers=self.headers["outsider"]).status_code, 404)
        draft = self.client.post(path + "/minutes", headers=self.headers["employee"])
        self.assertEqual(draft.status_code, 200)
        self.assertEqual(draft.json()["source_count"], 60)
        self.assertTrue(draft.json()["truncated"])
        self.assertEqual(self.minutes_model.received[0]["text"], "议题 5")
        self.assertEqual(draft.json()["discussion"][0]["quote"], "议题 5")
        self.client.delete(path + "/members/employee-1", headers=self.headers["manager"])
        self.assertEqual(self.client.post(path + "/minutes", headers=self.headers["employee"]).status_code, 404)

    def test_minutes_reject_fabricated_quote(self):
        room_id = self.create()
        path = f"/api/v1/meeting-rooms/{room_id}"
        self.client.post(path + "/messages", headers=self.headers["manager"],
                         json={"text": "尚未作出决定"})
        self.minutes_model.select = lambda messages: {
            "discussion": [], "decisions": [{"message_id": messages[0]["id"], "quote": "已经批准"}],
            "actions": []}
        response = self.client.post(path + "/minutes", headers=self.headers["manager"])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "MINUTES_MODEL_INVALID")

    def test_repository_persists_members_and_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rooms.sqlite3"
            manager = self.accounts.list_accounts()[1][1]
            first = MeetingRoomRepository(path)
            room = first.create(manager, "product", "产品部会议室")
            first.add_member(manager, room["id"], "employee-1")
            first.post(manager, room["id"], "记录保留")
            first.close()
            reopened = MeetingRoomRepository(path)
            employee = next(subject for name, subject in self.accounts.list_accounts() if name == "employee")
            self.assertEqual(reopened.messages(employee, room["id"])[0]["text"], "记录保留")
            reopened.close()


if __name__ == "__main__":
    unittest.main()
