"""One-time local demo cutover from SQLite conversations to PostgreSQL.

This is an explicit maintenance operation. It retains a local encrypted
pre-cutover backup and restores the prior API configuration on startup failure.
"""

import argparse
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from dotenv import dotenv_values, set_key
from psycopg.conninfo import make_conninfo

from eval.backup_memory_bundle import ROOT, capture, pack, run, verify, wait_api
from eval.migrate_sqlite_conversations import apply_snapshot, source_snapshot


def product_dsn(config: dict) -> str:
    return make_conninfo(host="127.0.0.1", port=55434, dbname="agent_memory",
                         user="agent_memory",
                         password=config.get("MEMORY_STORE_PASSWORD")
                         or "local-memory-store-only", connect_timeout=3)


def cutover() -> dict:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        raise RuntimeError("LOCAL_ENV_REQUIRED")
    original_env = env_path.read_bytes()
    config = dotenv_values(env_path)
    if config.get("MEMORY_BACKEND") != "postgres":
        raise RuntimeError("POSTGRES_MEMORY_BACKEND_REQUIRED")
    if config.get("CONVERSATION_BACKEND", "sqlite") != "sqlite":
        raise RuntimeError("CONVERSATION_BACKEND_ALREADY_CHANGED")
    dsn = product_dsn(config)
    with psycopg.connect(dsn) as connection:
        if connection.execute("SELECT current_database()").fetchone()[0] != "agent_memory":
            raise RuntimeError("PRODUCT_MEMORY_DATABASE_MISMATCH")
    wait_api()
    run("docker", "compose", "build", "api")
    backup_dir = ROOT / "data" / ("conversation-cutover-" +
                                  datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") +
                                  "-" + uuid4().hex[:6])
    backup_dir.mkdir(parents=True, exist_ok=False)
    passphrase = secrets.token_urlsafe(40)
    key_path = backup_dir / "local-recovery-key.txt"
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as key_file:
        key_file.write(passphrase)
    bundle_path = backup_dir / "before-cutover.eab"
    api_stopped = False
    config_changed = False
    cutover_committed = False
    try:
        with tempfile.TemporaryDirectory(prefix="conversation-cutover-",
                                         dir=ROOT / "data") as temporary:
            work = Path(temporary)
            api_stopped = True
            manifest = capture(work, resume_api=False)
            if manifest["authoritative_conversation_backend"] != "sqlite":
                raise RuntimeError("SOURCE_BACKEND_CHANGED_DURING_CUTOVER")
            backup = pack(work, bundle_path, passphrase, manifest)
            verify_directory = work / "verify"
            verify_directory.mkdir()
            verified = verify(bundle_path, passphrase, verify_directory)
            if not verified["verified"]:
                raise RuntimeError("PRE_CUTOVER_BACKUP_NOT_VERIFIED")
            snapshot = work / "conversations.sqlite3"
            _, source_tables, source_hash = source_snapshot(snapshot)
            imported = apply_snapshot(snapshot, dsn, schema="memory",
                                      expected_sha256=source_hash,
                                      target_database="agent_memory")
            if imported["tables"] != source_tables:
                raise RuntimeError("CONVERSATION_IMPORT_MISMATCH")
            config_changed = True
            set_key(str(env_path), "CONVERSATION_BACKEND", "postgres")
            run("docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "api")
            wait_api()
            cutover_committed = True
            run("docker", "compose", "--profile", "monitor", "up", "-d",
                "--no-deps", "--force-recreate", "memory-monitor")
            result = {"dataset": "local-conversation-postgres-cutover-v1",
                      "source_sha256": source_hash,
                      "tables": source_tables,
                      "encrypted_backup_sha256": backup["bundle_sha256"],
                      "backup_verified": verified["verified"],
                      "api_ready": True,
                      "conversation_backend": "postgres",
                      "backup_directory": str(backup_dir),
                      "verified": True}
            (backup_dir / "cutover-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return result
    except BaseException:
        if config_changed and not cutover_committed:
            env_path.write_bytes(original_env)
        if api_stopped and not cutover_committed:
            run("docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "api")
            wait_api()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.run:
        raise ValueError("EXPLICIT_RUN_FLAG_REQUIRED")
    result = cutover()
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
