"""Read and delete a temporary conversation across two local API containers."""

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
FIRST = "http://127.0.0.1:8001"
SECOND = "http://127.0.0.1:8002"


def call(base: str, method: str, path: str, *, token: str | None = None,
         body: dict | None = None) -> tuple[int, dict | None]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        base + path, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        error.read()
        return error.code, None


def login(base: str) -> str:
    status, body = call(base, "POST", "/api/v1/auth/login",
                        body={"username": "employee", "password": "Employee@2026"})
    if status != 200:
        raise RuntimeError("DEMO_LOGIN_FAILED")
    return body["access_token"]


def wait_ready(base: str, timeout: float = 45) -> None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if call(base, "GET", "/readyz")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("SECOND_API_NOT_READY")


def evaluate() -> dict:
    name = "enterprise-agent-api-eval-" + uuid4().hex[:8]
    command = ["docker", "compose", "run", "--no-deps", "-d", "--name", name,
               "-p", "127.0.0.1:8002:8000", "api"]
    subprocess.run(command, check=True, capture_output=True, text=True, cwd=ROOT)
    conversation_id = None
    first_token = None
    try:
        wait_ready(SECOND)
        first_token = login(FIRST)
        second_token = login(SECOND)
        created_status, created = call(
            FIRST, "POST", "/api/v1/conversations/answers", token=first_token,
            body={"question": "你好", "mode": "extract"})
        if created_status != 200:
            raise RuntimeError("FIRST_API_CREATE_FAILED")
        conversation_id = created["conversation_id"]
        other_status, other = call(
            SECOND, "GET", f"/api/v1/conversations/{conversation_id}",
            token=second_token)
        if other_status != 200 or other["turns"][-1]["question"] != "你好":
            raise RuntimeError("SECOND_API_READ_FAILED")
        deleted_status, _ = call(
            SECOND, "DELETE", f"/api/v1/conversations/{conversation_id}",
            token=second_token)
        after_status, _ = call(
            FIRST, "GET", f"/api/v1/conversations/{conversation_id}",
            token=first_token)
        if deleted_status != 204 or after_status != 404:
            raise RuntimeError("CROSS_INSTANCE_DELETE_FAILED")
        conversation_id = None
        return {"dataset": "local-pg-conversation-two-api-v1",
                "first_create": created_status, "second_read": other_status,
                "second_delete": deleted_status, "first_after_delete": after_status,
                "verified": True}
    finally:
        if conversation_id and first_token:
            call(FIRST, "DELETE", f"/api/v1/conversations/{conversation_id}",
                 token=first_token)
        subprocess.run(["docker", "rm", "-f", name],
                       check=True, capture_output=True, text=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.run:
        raise ValueError("EXPLICIT_RUN_FLAG_REQUIRED")
    result = evaluate()
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
