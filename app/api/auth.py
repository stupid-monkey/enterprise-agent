"""Local development authentication; replace with enterprise OIDC for production."""

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.security.policy import Classification, Subject, UserRole


_DEV_ACCOUNT_ROWS = (
    ("employee", "scrypt$16384$8$1$59f565ee9f79635d5c2b77324fd8e384$2c767b6a432d509733abac505891c788daa1dd84773583fe19dcc45d70995427ef1bb27786af3b1edf563935ad4ee5942db17fb1c7c801ba0291558db4ab0e42", "employee-001", "general", Classification.INTERNAL, UserRole.EMPLOYEE),
    ("manager", "scrypt$16384$8$1$031974d90f641cf049e71507cf8bd258$5895d28f4db4b23a5b86b30f108ee60bebb3d4e675f5014a06b88ed551b19dd2dc56f4717b39b8b36994eee5d2f74c330f9e6c8a5950d5477143af83fc5a94b5", "manager-001", "product", Classification.CONFIDENTIAL, UserRole.DEPARTMENT_MANAGER),
    ("knowledge", "scrypt$16384$8$1$fdf92cdd9f2f7296ec33585c185ba662$0606170b8b2bdf0ab10ae1f7b7c427f665cf005a4bb3344b25c321b6045ce1390a01a51976d0353abef57f1ae31ff458ac4f38d6a1f91d9ae3b7c29562e8e837", "knowledge-001", "operations", Classification.RESTRICTED, UserRole.KNOWLEDGE_ADMIN),
    ("admin", "scrypt$16384$8$1$2a0bbacfc207592f6b35b838bd854276$2b728de2b5d5d4ae3e9f846314f53f8d47638084c939ae8763031979e2d264c510595bb44406f2cf639854782544e5fb9458673a3145a2514bacdd4e35d25966", "admin-001", "it", Classification.RESTRICTED, UserRole.SYSTEM_ADMIN),
)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class LocalAccount:
    username: str
    password_hash: str
    subject: Subject


class LocalAuth:
    def __init__(self, accounts: list[LocalAccount], *, session_seconds: int = 3600):
        self._accounts = {account.username: account for account in accounts}
        self._sessions: dict[str, tuple[Subject, float]] = {}
        self._session_seconds = session_seconds

    @property
    def configured(self) -> bool:
        return bool(self._accounts)

    @staticmethod
    def _token_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def login(self, username: str, password: str) -> str | None:
        account = self._accounts.get(username)
        if account is None or not account.subject.active:
            return None
        if not verify_password(password, account.password_hash):
            return None
        token = secrets.token_urlsafe(32)
        self._sessions[self._token_key(token)] = (account.subject, time.time() + self._session_seconds)
        return token

    def resolve(self, token: str) -> Subject | None:
        key = self._token_key(token)
        session = self._sessions.get(key)
        if session is None:
            return None
        subject, expires_at = session
        if time.time() >= expires_at or not subject.active:
            self._sessions.pop(key, None)
            return None
        return subject

    def logout(self, token: str) -> None:
        self._sessions.pop(self._token_key(token), None)

    def list_accounts(self) -> list[tuple[str, Subject]]:
        return [(name, account.subject) for name, account in sorted(self._accounts.items())]


def account_from_environment() -> list[LocalAccount]:
    """Local fixtures require both a local environment and an explicit switch."""
    import os

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    accounts: list[LocalAccount] = []
    if os.getenv("APP_ENV", "").lower() == "local" and os.getenv("ENABLE_DEV_ACCOUNTS", "").lower() == "true":
        for username, password_hash, user_id, department, clearance, role in _DEV_ACCOUNT_ROWS:
            subject = Subject(user_id, "demo-org", frozenset({department}), clearance, role=role)
            accounts.append(LocalAccount(username, password_hash, subject))
    username = os.getenv("DEMO_USERNAME", "").strip()
    password_hash = os.getenv("DEMO_PASSWORD_HASH", "").strip()
    user_id = os.getenv("DEMO_USER_ID", "").strip()
    org_id = os.getenv("DEMO_ORG_ID", "").strip()
    if not all((username, password_hash, user_id, org_id)):
        return accounts
    try:
        clearance = Classification[os.getenv("DEMO_CLEARANCE", "INTERNAL").upper()]
        role = UserRole(os.getenv("DEMO_ROLE", "employee").lower())
    except (KeyError, ValueError):
        return accounts
    departments = frozenset(item.strip() for item in os.getenv("DEMO_DEPARTMENTS", "").split(",") if item.strip())
    subject = Subject(user_id=user_id, org_id=org_id, department_ids=departments, clearance=clearance, role=role)
    accounts = [account for account in accounts if account.username != username]
    accounts.append(LocalAccount(username=username, password_hash=password_hash, subject=subject))
    return accounts
