import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, account_from_environment, hash_password, verify_password
from app.api.main import create_app
from app.security.policy import Classification, Subject, UserRole


class ApiTests(unittest.TestCase):
    def setUp(self):
        subject = Subject("employee-1", "org-a", frozenset({"finance"}), Classification.DEPARTMENT)
        account = LocalAccount("alice", hash_password("correct-password"), subject)
        self.auth = LocalAuth([account])
        self.client = TestClient(create_app(self.auth))

    def test_health_and_trace_header(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.headers["X-Trace-Id"]), 32)
        self.assertEqual(self.client.get("/readyz").status_code, 200)

    def test_me_requires_valid_session(self):
        denied = self.client.get("/api/v1/me")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["code"], "UNAUTHENTICATED")
        self.assertEqual(denied.json()["trace_id"], denied.headers["X-Trace-Id"])

        token = self.client.post("/api/v1/auth/login", json={"username": "alice", "password": "correct-password"}).json()["access_token"]
        accepted = self.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["org_id"], "org-a")
        self.assertEqual(accepted.json()["department_ids"], ["finance"])

    def test_bad_password_and_forged_token_are_rejected(self):
        bad = self.client.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong"})
        self.assertEqual(bad.status_code, 401)
        forged = self.client.get("/api/v1/me", headers={"Authorization": "Bearer forged"})
        self.assertEqual(forged.status_code, 401)

    def test_logout_revokes_session(self):
        token = self.client.post("/api/v1/auth/login", json={"username": "alice", "password": "correct-password"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.post("/api/v1/auth/logout", headers=headers).status_code, 204)
        self.assertEqual(self.client.get("/api/v1/me", headers=headers).status_code, 401)

    def test_forged_logout_is_rejected(self):
        response = self.client.post("/api/v1/auth/logout", headers={"Authorization": "Bearer forged"})
        self.assertEqual(response.status_code, 401)

    def test_expired_session_is_rejected(self):
        token = self.auth.login("alice", "correct-password")
        key = self.auth._token_key(token)
        subject, _ = self.auth._sessions[key]
        self.auth._sessions[key] = (subject, 0)
        response = self.client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 401)

    def test_no_configured_accounts_fail_closed(self):
        client = TestClient(create_app(LocalAuth([])))
        self.assertEqual(client.get("/readyz").status_code, 503)
        response = client.post("/api/v1/auth/login", json={"username": "alice", "password": "correct-password"})
        self.assertEqual(response.status_code, 401)

    def test_password_hash_uses_random_salt(self):
        first = hash_password("correct-password")
        second = hash_password("correct-password")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct-password", first))
        self.assertFalse(verify_password("wrong", first))

    def test_dev_accounts_require_explicit_local_switch(self):
        with patch.dict("os.environ", {"APP_ENV": "production", "ENABLE_DEV_ACCOUNTS": "true"}):
            self.assertFalse(any(account.username == "admin" for account in account_from_environment()))
        with patch.dict("os.environ", {"APP_ENV": "local", "ENABLE_DEV_ACCOUNTS": "false"}):
            self.assertFalse(any(account.username == "admin" for account in account_from_environment()))

    def test_four_dev_roles_and_admin_boundary(self):
        with patch.dict("os.environ", {"APP_ENV": "local", "ENABLE_DEV_ACCOUNTS": "true"}):
            accounts = account_from_environment()
        client = TestClient(create_app(LocalAuth(accounts)))
        expected_roles = {
            "employee": ("Employee@2026", "employee"),
            "manager": ("Manager@2026", "department_manager"),
            "knowledge": ("Knowledge@2026", "knowledge_admin"),
            "admin": ("Admin@2026", "system_admin"),
        }
        for username, (password, role) in expected_roles.items():
            with self.subTest(username=username):
                login = client.post("/api/v1/auth/login", json={"username": username, "password": password})
                self.assertEqual(login.status_code, 200)
                headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
                self.assertEqual(client.get("/api/v1/me", headers=headers).json()["role"], role)
                directory = client.get("/api/v1/admin/accounts", headers=headers)
                self.assertEqual(directory.status_code, 200 if username == "admin" else 403)
                if username == "admin":
                    self.assertEqual({item["username"] for item in directory.json()}, set(expected_roles))
                    self.assertNotIn("password_hash", directory.text)

    def test_admin_directory_stays_in_organization(self):
        admin = LocalAccount("admin", hash_password("secret"), Subject("admin", "org-a", frozenset(), Classification.RESTRICTED, role=UserRole.SYSTEM_ADMIN))
        outsider = LocalAccount("outsider", hash_password("secret"), Subject("other", "org-b", frozenset(), Classification.INTERNAL))
        client = TestClient(create_app(LocalAuth([admin, outsider])))
        token = client.post("/api/v1/auth/login", json={"username": "admin", "password": "secret"}).json()["access_token"]
        response = client.get("/api/v1/admin/accounts", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual([item["username"] for item in response.json()], ["admin"])


if __name__ == "__main__":
    unittest.main()
