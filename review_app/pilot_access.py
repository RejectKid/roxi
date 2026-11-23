import hashlib
import hmac
import os
import re
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .live_chat import connect_editor
from .storage import connect_read

COOKIE = "roxi_session"
SESSION_SECONDS = 8 * 60 * 60
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}")
NAMESPACE = re.compile(r"(?:(?:chat|catalog|recorded|map):[a-z0-9]+(?:-[a-z0-9]+)*|history:registry)")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
READ_ROLES = {"admin", "reviewer", "reader"}
ROLES = {*READ_ROLES, "ingest"}
AUTH = ContextVar("roxi_auth", default=None)
_DUMMY_SALT = bytes(16)


@dataclass
class AccessError(Exception):
    status: int
    detail: str


@dataclass(frozen=True)
class Grant:
    role: str
    sources: frozenset[str] | None


def demo() -> bool:
    return os.environ.get("ROXI_ACCESS_MODE") == "demo"


def credential_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def credential(token: str | None, kind: str) -> tuple[str, str] | None:
    if token and TOKEN.fullmatch(token):
        return credential_digest(token), kind
    return None


def identity(conn, auth: tuple[str, str] | None = None) -> str | None:
    auth = auth or AUTH.get()
    if auth is None:
        return None
    row = conn.execute(
        "SELECT username FROM pilot_credential WHERE digest = %s AND kind = %s AND expires_at > clock_timestamp()",
        auth,
    ).fetchone()
    return row["username"] if row else None


def require(exercise_id: str, *, full: bool = True, source: str | None = None, role: str = "read") -> Grant:
    limit = os.environ.get("EXERCISE_REVIEW_API_EXERCISES")
    hidden = AccessError(404, "Exercise or source not found.")
    if limit is not None and exercise_id not in {entry.strip() for entry in limit.split(",")}:
        raise hidden
    if demo():
        with connect_read() as conn:
            if conn.execute("SELECT 1 FROM pilot_exercise WHERE exercise_id = %s", (exercise_id,)).fetchone():
                raise hidden
        return Grant("admin", None)
    with connect_read() as conn:
        username = identity(conn)
        if not username:
            raise AccessError(401, "Sign in or provide a valid access token.")
        row = conn.execute(
            """
            SELECT m.role, m.all_sources, e.closed_at,
                   ARRAY(SELECT source FROM pilot_source_grant
                         WHERE workspace_id = m.workspace_id AND username = m.username
                           AND exercise_id = e.exercise_id) AS sources
            FROM pilot_exercise AS e JOIN pilot_member AS m USING (workspace_id)
            WHERE e.exercise_id = %s AND m.username = %s
            """,
            (exercise_id, username),
        ).fetchone()
        roles = {"read": READ_ROLES, "review": {"admin", "reviewer"}, "ingest": {"admin", "ingest"}, "admin": {"admin"}}
        if not row or row["role"] not in roles[role] or (row["closed_at"] and role in {"review", "ingest"}):
            raise hidden
        sources = None if row["all_sources"] else frozenset(row["sources"])
        if sources is not None and (not sources or full or source is not None and source not in sources):
            raise hidden
        return Grant(row["role"], sources)


def visible_exercises() -> list[dict]:
    with connect_read() as conn:
        username = identity(conn)
        if not username:
            return []
        rows = conn.execute(
            """
            SELECT w.id AS workspace_id, w.name AS workspace, e.id, e.name, m.role, m.all_sources, pe.closed_at
            FROM pilot_member AS m JOIN pilot_workspace AS w ON w.id = m.workspace_id
            JOIN pilot_exercise AS pe ON pe.workspace_id = m.workspace_id JOIN exercise AS e ON e.id = pe.exercise_id
            WHERE m.username = %s AND m.role IN ('admin', 'reviewer', 'reader')
              AND (m.all_sources OR EXISTS (
                SELECT 1 FROM pilot_source_grant AS g
                WHERE g.workspace_id = m.workspace_id AND g.username = m.username AND g.exercise_id = e.id))
            ORDER BY w.name, e.name, e.id
            """,
            (username,),
        ).fetchall()
    limit = os.environ.get("EXERCISE_REVIEW_API_EXERCISES")
    return rows if limit is None else [row for row in rows if row["id"] in {key.strip() for key in limit.split(",")}]


def require_ingest(exercise_id: str, source: str) -> Grant:
    auth = AUTH.get()
    if demo() or not auth or auth[1] != "api":
        raise AccessError(401, "Provide a workspace ingestion token.")
    return require(exercise_id, full=False, source=source, role="ingest")


def password_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)


def password_hash(password: str) -> str:
    if not 12 <= len(password) <= 256 or len(password.encode()) > 1024:
        raise ValueError("Use a password of 12 to 256 characters.")
    salt = secrets.token_bytes(16)
    return f"{salt.hex()}:{password_key(password, salt).hex()}"


def issue(conn, username: str, kind: str, duration: timedelta) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM pilot_credential WHERE expires_at <= clock_timestamp()")
    conn.execute(
        "INSERT INTO pilot_credential (digest, username, kind, expires_at) VALUES (%s, %s, %s, %s)",
        (credential_digest(token), username, kind, datetime.now(UTC) + duration),
    )
    return token


def login(username: str, password: str, peer: str) -> str | None:
    with connect_editor() as conn:
        conn.execute("DELETE FROM pilot_login_budget WHERE reset_at <= clock_timestamp()")
        budget = conn.execute(
            """
            INSERT INTO pilot_login_budget (peer, reset_at, attempts)
            VALUES (%s, clock_timestamp() + interval '5 minutes', 1)
            ON CONFLICT (peer) DO UPDATE SET
                attempts = CASE WHEN pilot_login_budget.reset_at <= clock_timestamp()
                           THEN 1 ELSE LEAST(pilot_login_budget.attempts + 1, 21) END,
                reset_at = CASE WHEN pilot_login_budget.reset_at <= clock_timestamp()
                           THEN EXCLUDED.reset_at ELSE pilot_login_budget.reset_at END
            RETURNING attempts
            """,
            (peer,),
        ).fetchone()
    if budget["attempts"] > 20:
        raise AccessError(429, "Too many sign-in attempts. Try again in five minutes.")
    with connect_editor() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("roxi-login:" + username,))
        row = conn.execute("SELECT password_hash FROM pilot_account WHERE username = %s",
                           (username,)).fetchone() if NAME.fullmatch(username) else None
        salt, expected = (map(bytes.fromhex, row["password_hash"].split(":")) if row
                          else (_DUMMY_SALT, bytes(64)))
        valid = hmac.compare_digest(password_key(password, salt), expected)
        return issue(conn, username, "session", timedelta(seconds=SESSION_SECONDS)) if row and valid else None


def logout() -> None:
    auth = AUTH.get()
    if auth and auth[1] == "session":
        with connect_editor() as conn:
            conn.execute("DELETE FROM pilot_credential WHERE digest = %s AND kind = 'session'", (auth[0],))
