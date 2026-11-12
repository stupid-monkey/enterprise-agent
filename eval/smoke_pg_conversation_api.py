"""Exercise one temporary chat through the switched local Docker API."""

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path


BASE = "http://127.0.0.1:8001"


def request(method: str, path: str, *, token: str | None = None,
            body: dict | None = None) -> tuple[int, dict | None]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = json.dumps(body).encode() if body is not None else None
    call = urllib.request.Request(BASE + path, data=payload, headers=headers,
                                  method=method)
    try:
        with urllib.request.urlopen(call, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        error.read()
        return error.code, None


def login(username: str, password: str) -> str:
    status, body = request("POST", "/api/v1/auth/login",
                           body={"username": username, "password": password})
    if status != 200:
        raise RuntimeError("DEMO_LOGIN_FAILED")
    return body["access_token"]


def smoke() -> dict:
    employee = login("employee", "Employee@2026")
    manager = login("manager", "Manager@2026")
    status, created = request("POST", "/api/v1/conversations/answers",
                              token=employee,
                              body={"question": "你好", "mode": "extract"})
    if status != 200:
        raise RuntimeError("CONVERSATION_CREATE_FAILED")
    conversation_id = created["conversation_id"]
    try:
        own_status, own = request("GET", f"/api/v1/conversations/{conversation_id}",
                                  token=employee)
        other_status, _ = request("GET", f"/api/v1/conversations/{conversation_id}",
                                  token=manager)
        if (own_status != 200 or other_status != 404
                or own["turns"][-1]["question"] != "你好"):
            raise RuntimeError("CONVERSATION_READ_ISOLATION_FAILED")
    finally:
        deleted_status, _ = request("DELETE", f"/api/v1/conversations/{conversation_id}",
                                    token=employee)
    hidden_status, _ = request("GET", f"/api/v1/conversations/{conversation_id}",
                               token=employee)
    if deleted_status != 204 or hidden_status != 404:
        raise RuntimeError("CONVERSATION_DELETE_FAILED")
    return {"dataset": "local-pg-conversation-api-smoke-v1",
            "created": status == 200, "owner_read": own_status == 200,
            "cross_account_status": other_status, "delete_status": deleted_status,
            "after_delete_status": hidden_status, "verified": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.run:
        raise ValueError("EXPLICIT_RUN_FLAG_REQUIRED")
    result = smoke()
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
