"""Encrypted local backup and isolated restore drill for the two memory stores.

The API is stopped while SQLite and PostgreSQL are sampled. The live databases
are never restored in place. A drill uses an ephemeral key and deletes its bundle.
"""

import argparse
from contextlib import closing
import hashlib
import http.client
import io
import json
import os
import secrets
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


ROOT = Path(__file__).resolve().parents[1]
MAGIC = b"EAMEM1"
PG_TABLES = ("memory_schema_version", "memory_queue_schema_version",
             "memory_fact", "memory_source", "memory_setting",
             "memory_suppression", "memory_job", "memory_job_effect",
             "memory_job_failure", "memory_fact_guard", "memory_suggestion",
             "memory_enqueue_receipt", "memory_import_manifest")
CONVERSATION_TABLES = ("conversation_schema_version", "conversations",
                       "conversation_turns", "conversation_delete_outbox",
                       "conversation_memory_outbox")
SQLITE_TABLES = ("conversations", "conversation_turns",
                 "conversation_memory_outbox", "conversation_delete_outbox")


def run(*arguments: str) -> str:
    return subprocess.run(arguments, check=True, capture_output=True, text=True).stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def counts(container: str, user: str, database: str, tables: tuple[str, ...],
           *, schema: str | None = None) -> dict[str, int]:
    result = {}
    for table in tables:
        name = f"{schema}.{table}" if schema else table
        result[table] = int(run("docker", "exec", container, "psql", "-U", user,
                                "-d", database, "-At", "-c",
                                f"SELECT count(*) FROM {name}"))
    return result


def content_digests(container: str, user: str, database: str,
                    tables: tuple[str, ...]) -> dict[str, str]:
    result = {}
    for table in tables:
        query = ("SELECT md5(COALESCE(jsonb_agg(to_jsonb(t) "
                 "ORDER BY to_jsonb(t)::text)::text, '[]')) "
                 f"FROM memory.{table} t")
        result[table] = run("docker", "exec", container, "psql", "-U", user,
                            "-d", database, "-At", "-c", query)
    return result


def sqlite_counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("MEMORY_SQLITE_INTEGRITY_FAILED")
        return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in SQLITE_TABLES}


def key_for(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 16:
        raise ValueError("MEMORY_BACKUP_PASSPHRASE_TOO_SHORT")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


def encrypt(source: Path, target: Path, passphrase: str) -> None:
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    header = MAGIC + salt + nonce
    cipher = Cipher(algorithms.AES(key_for(passphrase, salt)), modes.GCM(nonce))
    operation = cipher.encryptor()
    operation.authenticate_additional_data(header)
    with source.open("rb") as plain, target.open("xb") as encrypted:
        encrypted.write(header)
        for block in iter(lambda: plain.read(1024 * 1024), b""):
            encrypted.write(operation.update(block))
        encrypted.write(operation.finalize())
        encrypted.write(operation.tag)


def decrypt(source: Path, target: Path, passphrase: str) -> None:
    with source.open("rb") as encrypted:
        header = encrypted.read(len(MAGIC) + 28)
        if len(header) != len(MAGIC) + 28 or not header.startswith(MAGIC):
            raise ValueError("INVALID_MEMORY_BACKUP_FORMAT")
        salt = header[len(MAGIC):len(MAGIC) + 16]
        nonce = header[-12:]
        encrypted.seek(-16, os.SEEK_END)
        tag = encrypted.read(16)
        remaining = source.stat().st_size - len(header) - len(tag)
        operation = Cipher(algorithms.AES(key_for(passphrase, salt)),
                           modes.GCM(nonce)).decryptor()
        operation.authenticate_additional_data(header)
        encrypted.seek(len(header))
        with target.open("xb") as plain:
            while remaining:
                block = encrypted.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("TRUNCATED_MEMORY_BACKUP")
                plain.write(operation.update(block))
                remaining -= len(block)
            plain.write(operation.finalize_with_tag(tag))


def wait_api(timeout: float = 45) -> None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8001/readyz", timeout=3) as reply:
                if reply.status == 200:
                    return
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            pass
        time.sleep(0.5)
    raise RuntimeError("API_NOT_READY_AFTER_BACKUP")


def capture(directory: Path, *, resume_api: bool = True) -> dict:
    running = set(run("docker", "compose", "ps", "--services", "--status", "running").splitlines())
    if not {"api", "memory-store"} <= running:
        raise RuntimeError("MEMORY_BACKUP_SERVICES_REQUIRED")
    raw = directory / "raw.sqlite3"
    sqlite_copy = directory / "conversations.sqlite3"
    dump = directory / "memory.dump"
    conversation_backend = run("docker", "exec", "enterprise-agent-api-1", "python",
                               "-c", "import os; print(os.getenv('CONVERSATION_BACKEND', 'sqlite'))")
    if conversation_backend not in ("sqlite", "postgres"):
        raise RuntimeError("INVALID_RUNNING_CONVERSATION_BACKEND")
    tables = PG_TABLES + (CONVERSATION_TABLES if conversation_backend == "postgres" else ())
    stopped = False
    started = time.monotonic()
    try:
        stopped = True
        run("docker", "compose", "stop", "api")
        run("docker", "cp", "enterprise-agent-api-1:/app/data/demo_conversations.sqlite3",
            str(raw))
        for suffix in ("-wal", "-shm"):
            subprocess.run(["docker", "cp",
                            "enterprise-agent-api-1:/app/data/demo_conversations.sqlite3" + suffix,
                            str(raw) + suffix], capture_output=True)
        with closing(sqlite3.connect(raw)) as source, closing(sqlite3.connect(sqlite_copy)) as target:
            source.backup(target)
        source_counts = sqlite_counts(sqlite_copy)
        with dump.open("wb") as output:
            subprocess.run(["docker", "exec", "enterprise-agent-memory-store-1",
                            "pg_dump", "-U", "agent_memory", "-d", "agent_memory", "-Fc"],
                           stdout=output, stderr=subprocess.PIPE, check=True)
        with dump.open("rb") as source:
            if source.read(5) != b"PGDMP":
                raise RuntimeError("INVALID_MEMORY_PG_DUMP")
        pg_counts = counts("enterprise-agent-memory-store-1", "agent_memory",
                           "agent_memory", tables, schema="memory")
        pg_digests = content_digests("enterprise-agent-memory-store-1",
                                     "agent_memory", "agent_memory", tables)
    finally:
        if stopped and resume_api:
            run("docker", "compose", "up", "-d", "--no-deps", "api")
    if resume_api:
        wait_api()
    return {"captured_at": datetime.now(timezone.utc).isoformat(),
            "capture_seconds": round(time.monotonic() - started, 3),
            "authoritative_conversation_backend": conversation_backend,
            "postgres_tables": tables,
            "sqlite_counts": source_counts, "postgres_counts": pg_counts,
            "postgres_content_md5": pg_digests,
            "sha256": {"conversations.sqlite3": sha256(sqlite_copy),
                       "memory.dump": sha256(dump)}}


def pack(directory: Path, output: Path, passphrase: str, manifest: dict) -> dict:
    archive = directory / "bundle.tar.gz"
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(encoded)
        tar.addfile(info, io.BytesIO(encoded))
        for name in ("conversations.sqlite3", "memory.dump"):
            tar.add(directory / name, arcname=name)
    encrypt(archive, output, passphrase)
    return {"bundle_sha256": sha256(output), "bundle_bytes": output.stat().st_size,
            "capture_seconds": manifest["capture_seconds"]}


def bundle(directory: Path, output: Path, passphrase: str) -> dict:
    return pack(directory, output, passphrase, capture(directory))


def verify(source: Path, passphrase: str, directory: Path) -> dict:
    running = set(run("docker", "compose", "ps", "--services", "--status", "running").splitlines())
    if "memory-db" not in running:
        raise RuntimeError("MEMORY_LAB_SERVICE_REQUIRED")
    archive = directory / "restored.tar.gz"
    decrypt(source, archive, passphrase)
    with tarfile.open(archive, "r:gz") as tar:
        if set(tar.getnames()) != {"manifest.json", "conversations.sqlite3", "memory.dump"}:
            raise RuntimeError("INVALID_MEMORY_BACKUP_MEMBERS")
        manifest = json.load(tar.extractfile("manifest.json"))
        for name in ("conversations.sqlite3", "memory.dump"):
            with (directory / name).open("wb") as target:
                member = tar.extractfile(name)
                for block in iter(lambda: member.read(1024 * 1024), b""):
                    target.write(block)
            if sha256(directory / name) != manifest["sha256"][name]:
                raise RuntimeError("MEMORY_BACKUP_HASH_MISMATCH")
    if sqlite_counts(directory / "conversations.sqlite3") != manifest["sqlite_counts"]:
        raise RuntimeError("MEMORY_SQLITE_RESTORE_MISMATCH")
    database = "memory_restore_eval_" + uuid4().hex[:12]
    remote = "/tmp/" + database + ".dump"
    created = False
    started = time.monotonic()
    try:
        run("docker", "cp", str(directory / "memory.dump"),
            "enterprise-agent-memory-db-1:" + remote)
        run("docker", "exec", "enterprise-agent-memory-db-1", "createdb", "-U",
            "agent_memory_lab", "-O", "agent_memory_lab", database)
        created = True
        run("docker", "exec", "enterprise-agent-memory-db-1", "pg_restore",
            "-U", "agent_memory_lab", "-d", database, "--no-owner", "--no-acl", remote)
        restored = counts("enterprise-agent-memory-db-1", "agent_memory_lab",
                          database, tuple(manifest["postgres_tables"]), schema="memory")
        restored_digests = content_digests("enterprise-agent-memory-db-1",
                                            "agent_memory_lab", database,
                                            tuple(manifest["postgres_tables"]))
        if (restored != manifest["postgres_counts"]
                or restored_digests != manifest["postgres_content_md5"]):
            raise RuntimeError("MEMORY_PG_RESTORE_MISMATCH")
    finally:
        if created:
            run("docker", "exec", "enterprise-agent-memory-db-1", "dropdb", "-U",
                "agent_memory_lab", database)
        subprocess.run(["docker", "exec", "enterprise-agent-memory-db-1", "rm", "-f", remote],
                       capture_output=True)
    return {"verified": True, "restore_seconds": round(time.monotonic() - started, 3),
            "postgres_content_match": True,
            "authoritative_conversation_backend": manifest["authoritative_conversation_backend"],
            "sqlite_counts": manifest["sqlite_counts"],
            "postgres_counts": manifest["postgres_counts"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--backup", type=Path, help="Encrypted output inside data/")
    action.add_argument("--verify", type=Path, help="Decrypt and restore into lab database")
    action.add_argument("--drill", action="store_true", help="Ephemeral encrypted backup and restore")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path, help="Drill result JSON")
    args = parser.parse_args()
    if not args.run:
        raise ValueError("EXPLICIT_RUN_FLAG_REQUIRED")
    if args.drill:
        passphrase = secrets.token_urlsafe(32)
    else:
        passphrase = os.getenv("MEMORY_BACKUP_PASSPHRASE", "")
        if len(passphrase) < 16:
            raise ValueError("MEMORY_BACKUP_PASSPHRASE_REQUIRED")
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="memory-backup-", dir=data) as temporary:
        directory = Path(temporary)
        if args.verify:
            result = verify(args.verify.resolve(), passphrase, directory)
        else:
            target = (directory / "drill.eab" if args.drill else args.backup.resolve())
            if not target.is_relative_to(data.resolve()):
                raise ValueError("MEMORY_BACKUP_PATH_MUST_BE_IN_DATA")
            target.parent.mkdir(parents=True, exist_ok=True)
            result = bundle(directory, target, passphrase)
            if args.drill:
                verify_directory = directory / "verify"
                verify_directory.mkdir()
                result.update(verify(target, passphrase, verify_directory))
                result["dataset"] = "local-encrypted-memory-bundle-drill-v1"
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")


if __name__ == "__main__":
    main()
