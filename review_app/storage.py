import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = Path(os.environ.get("EXERCISE_REVIEW_ARCHIVE", ROOT / ".local" / "archive")).resolve()
OWNER_DSN = os.environ.get(
    "EXERCISE_REVIEW_DATABASE_URL",
    "postgresql://review_owner:review-local-owner@127.0.0.1:55432/exercise_review",
)
READER_DSN = os.environ.get(
    "EXERCISE_REVIEW_READER_URL",
    "postgresql://review_reader:review-local-reader@127.0.0.1:55432/exercise_review",
)

EDITOR_DSN = os.environ.get(
    "EXERCISE_REVIEW_EDITOR_URL",
    "postgresql://review_editor:review-local-editor@127.0.0.1:55432/exercise_review",
)
ARCHIVE_LOCK = 538176467047


@dataclass(frozen=True)
class ArchivedFile:
    sha256: str
    byte_size: int


def archive_path(digest: str) -> Path:
    return ARCHIVE / digest[:2] / digest


def archive_file(source: Path) -> ArchivedFile:
    with source.open("rb") as stream:
        return archive_stream(stream, source.name)


def archive_capture(data: bytes) -> ArchivedFile:
    return archive_stream(BytesIO(data), "source capture", allow_empty=True)


def archive_stream(stream: BinaryIO, name: str, *, allow_empty: bool = False) -> ArchivedFile:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    temporary = None
    try:
        with NamedTemporaryFile(dir=ARCHIVE, delete=False) as dest:
            temporary = Path(dest.name)
            while chunk := stream.read(1024 * 1024):
                dest.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if not size and not allow_empty:
            raise ValueError(f"{name} is empty.")
        key = digest.hexdigest()
        target = archive_path(key)
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            try:
                os.replace(temporary, target)
            except PermissionError:
                if not target.exists():
                    raise
        return ArchivedFile(key, size)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def connect_read():
    connection = psycopg.connect(READER_DSN, connect_timeout=5, row_factory=dict_row)
    connection.read_only = True
    connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    return connection


def connect_owner(dsn: str | None = None, **kwargs):
    return psycopg.connect(dsn or OWNER_DSN, connect_timeout=5, row_factory=dict_row, **kwargs)


@contextmanager
def archive_lock(*, shared: bool, dsn: str):
    # The advisory lock spans the database commit and file work, including retention cleanup.
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as lock:
        if shared:
            lock.execute("SELECT pg_advisory_lock_shared(%s)", (ARCHIVE_LOCK,))
        else:
            lock.execute("SELECT pg_advisory_lock(%s)", (ARCHIVE_LOCK,))
        yield


def archive_writer(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with archive_lock(shared=True, dsn=EDITOR_DSN):
            return function(*args, **kwargs)
    return locked


def migrate() -> None:
    directory = ROOT / "db" / "migrations"
    with psycopg.connect(OWNER_DSN, connect_timeout=5) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('exercise_review_migrations'))")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {row[0] for row in conn.execute("SELECT name FROM schema_migration")}
        for migration in sorted(directory.glob("*.sql")):
            if migration.name not in applied:
                conn.execute(migration.read_text(encoding="utf-8"), prepare=False)
                conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (migration.name,))
