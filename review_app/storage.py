import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

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


@dataclass(frozen=True)
class ArchivedFile:
    sha256: str
    byte_size: int


def archive_path(digest: str) -> Path:
    return ARCHIVE / digest[:2] / digest


def archive_file(source: Path) -> ArchivedFile:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    temporary = None
    try:
        with source.open("rb") as src, NamedTemporaryFile(dir=ARCHIVE, delete=False) as dest:
            temporary = Path(dest.name)
            while chunk := src.read(1024 * 1024):
                dest.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if not size:
            raise ValueError(f"{source.name} is empty.")
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
