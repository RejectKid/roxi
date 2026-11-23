import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from psycopg import IsolationLevel
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from . import storage
from .pilot_retention import referenced_archive

DIGEST = re.compile(r"[a-f0-9]{64}")
FORMAT = 1


def digest_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def process(tool: str, dsn: str, compose: bool) -> tuple[list[str], dict]:
    settings = conninfo_to_dict(dsn)
    environment = os.environ.copy()
    if compose:
        if (settings.get("host"), settings.get("port")) not in {("127.0.0.1", "55432"), ("localhost", "55432")}:
            raise ValueError("Compose backup uses the local 55432 database in this Roxi checkout.")
        compose_cmd = ["docker", "compose", "exec", "-T", "db"]
        with storage.connect_owner(dsn) as conn:
            info = conn.execute("SELECT system_identifier::text AS system FROM pg_control_system()").fetchone()
        command = [*compose_cmd, "psql", "-XAt", "-U", settings["user"], "-d", settings["dbname"],
                   "-c", "SELECT system_identifier::text FROM pg_control_system()"]
        server = subprocess.run(command, cwd=storage.ROOT, capture_output=True, text=True)
        if server.returncode or server.stdout.strip() != info["system"]:
            raise ValueError("The target DSN and Compose PostgreSQL server differ.")
        return [*compose_cmd, tool, "--no-password", "-U", settings["user"], "-d", settings["dbname"]], environment
    if password := settings.pop("password", None):
        environment["PGPASSWORD"] = password
    return [tool, "--no-password", "--dbname", make_conninfo(**settings)], environment


def run(command: list[str], environment: dict, *, stdin=None, stdout=None) -> None:
    result = subprocess.run(command, cwd=storage.ROOT, env=environment, stdin=stdin, stdout=stdout,
                            stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "The PostgreSQL backup utility failed.")


def store_archived(source_root: Path, destination_root: Path, digests: list[str]) -> None:
    for digest in digests:
        source = source_root / digest[:2] / digest
        destination = destination_root / digest[:2] / digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if digest_file(destination) != digest:
            raise ValueError(f"Archive digest mismatch for {digest}.")


def backup(destination: Path, *, compose: bool = False) -> dict:
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("The backup destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    command, environment = process("pg_dump", storage.OWNER_DSN, compose)
    with TemporaryDirectory(prefix=".roxi-backup-", dir=destination.parent) as root:
        bundle = Path(root) / "bundle"
        archive = bundle / "archive"
        archive.mkdir(parents=True)
        database = bundle / "database.dump"
        with storage.archive_lock(shared=True, dsn=storage.OWNER_DSN):
            with storage.connect_owner() as conn:
                conn.isolation_level = IsolationLevel.REPEATABLE_READ
                conn.read_only = True
                snapshot = conn.execute("SELECT pg_export_snapshot() AS id").fetchone()["id"]
                digests = sorted(referenced_archive(conn))
                with database.open("wb") as stream:
                    run([*command, "--format=custom", "--snapshot", snapshot,
                         "--exclude-table-data=public.pilot_credential",
                         "--exclude-table-data=public.pilot_login_budget"], environment, stdout=stream)
            store_archived(storage.ARCHIVE, archive, digests)
        metadata = {"version": FORMAT, "created_at": datetime.now(UTC).isoformat(),
                    "database_sha256": digest_file(database), "archive_digests": digests}
        (bundle / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        bundle.rename(destination)
    return {"backup": str(destination), "originals": len(digests), "database_sha256": metadata["database_sha256"]}


def restore(bundle: Path, target_dsn: str, target_archive: Path, *, compose: bool = False) -> dict:
    target_archive = target_archive.resolve()
    if target_archive.exists():
        raise ValueError("The target archive must be a new path.")
    target_archive.parent.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    database = bundle / "database.dump"
    digests = metadata.get("archive_digests")
    if (metadata.get("version") != FORMAT or not isinstance(digests, list) or
            not all(isinstance(item, str) and DIGEST.fullmatch(item) for item in digests) or
            len(set(digests)) != len(digests) or digest_file(database) != metadata.get("database_sha256")):
        raise ValueError("The backup manifest or database checksum is invalid.")
    command, environment = process("pg_restore", target_dsn, compose)
    with storage.archive_lock(shared=False, dsn=target_dsn):
        with storage.connect_owner(target_dsn) as conn:
            if conn.execute("SELECT 1 FROM pg_class AS c JOIN pg_namespace AS n ON c.relnamespace = n.oid "
                            "WHERE n.nspname = 'public' LIMIT 1").fetchone():
                raise ValueError("Restore requires an empty isolated database.")
        with TemporaryDirectory(prefix=".roxi-restore-", dir=target_archive.parent) as root:
            staged = Path(root) / "archive"
            staged.mkdir()
            store_archived(bundle / "archive", staged, digests)
            with database.open("rb") as stream:
                run([*command, "--single-transaction", "--exit-on-error"], environment, stdin=stream)
            staged.rename(target_archive)
    return {"archive": str(target_archive), "originals": len(digests)}
