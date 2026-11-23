import os
from datetime import timedelta
from getpass import getpass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, TypeAdapter

from . import pilot_access as access
from .importer import Slug
from .storage import connect_owner

User = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=f"^{access.NAME.pattern}$", max_length=80)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Days = Annotated[int, Field(ge=1, le=3650)]
Role = Literal["admin", "reader", "reviewer", "ingest"]


def workspace(workspace_id: str, name: str) -> None:
    with connect_owner() as conn:
        conn.execute("INSERT INTO pilot_workspace (id, name) VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
                     (workspace_id, name))


def set_password(username: str, password: str) -> None:
    username = TypeAdapter(User).validate_python(username)
    stored = access.password_hash(password)
    with connect_owner() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("roxi-login:" + username,))
        conn.execute("INSERT INTO pilot_account VALUES (%s, %s) ON CONFLICT (username) DO UPDATE "
                     "SET password_hash = EXCLUDED.password_hash", (username, stored))
        conn.execute("DELETE FROM pilot_credential WHERE username = %s", (username,))


def attach(workspace_id: str, exercise_id: str) -> None:
    with connect_owner() as conn:
        row = conn.execute(
            "INSERT INTO pilot_exercise (workspace_id, exercise_id) VALUES (%s, %s) "
            "ON CONFLICT (exercise_id) DO UPDATE SET workspace_id = EXCLUDED.workspace_id "
            "WHERE pilot_exercise.workspace_id = EXCLUDED.workspace_id RETURNING exercise_id",
            (workspace_id, exercise_id),
        ).fetchone()
        if not row:
            raise ValueError("The exercise already belongs to another workspace.")


def member(workspace_id: str, username: str, role: str, all_sources: bool = False) -> None:
    with connect_owner() as conn:
        conn.execute(
            "INSERT INTO pilot_member VALUES (%s, %s, %s, %s) ON CONFLICT (workspace_id, username) DO UPDATE "
            "SET role = EXCLUDED.role, all_sources = EXCLUDED.all_sources", (workspace_id, username, role, all_sources),
        )


def remove_member(workspace_id: str, username: str) -> None:
    with connect_owner() as conn:
        conn.execute("DELETE FROM pilot_member WHERE workspace_id = %s AND username = %s", (workspace_id, username))


def grant_source(workspace_id: str, username: str, exercise_id: str, source: str) -> None:
    if not access.NAMESPACE.fullmatch(source):
        raise ValueError("Use a namespaced source key such as catalog:fleet or chat:receiving-dock.")
    with connect_owner() as conn:
        found = conn.execute(
            """
            SELECT 'chat:' || room_key AS source FROM live_room WHERE exercise_id = %(id)s
            UNION SELECT 'catalog:' || source_key FROM catalog_source WHERE exercise_id = %(id)s
            UNION SELECT 'recorded:' || source_key FROM source_file WHERE exercise_id = %(id)s
            UNION SELECT 'map:' || layer_key FROM history_layer WHERE exercise_id = %(id)s
            UNION SELECT 'history:registry' FROM history_registry WHERE exercise_id = %(id)s
            """, {"id": exercise_id},
        ).fetchall()
        if source not in {row["source"] for row in found}:
            raise ValueError("This source is not registered for the exercise.")
        conn.execute("INSERT INTO pilot_source_grant VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                     (workspace_id, username, exercise_id, source))


def revoke_source(workspace_id: str, username: str, exercise_id: str, source: str) -> None:
    with connect_owner() as conn:
        membership = conn.execute("SELECT all_sources FROM pilot_member WHERE workspace_id = %s AND username = %s FOR UPDATE",
                                  (workspace_id, username)).fetchone()
        if membership and membership["all_sources"]:
            raise ValueError("Set the membership to source-specific access before revoking an individual source.")
        conn.execute("DELETE FROM pilot_source_grant WHERE workspace_id = %s AND username = %s AND exercise_id = %s AND source = %s",
                     (workspace_id, username, exercise_id, source))


def issue_token(username: str, hours: int = 24) -> str:
    if not 1 <= hours <= 720:
        raise ValueError("Token lifetime must be between 1 and 720 hours.")
    with connect_owner() as conn:
        return access.issue(conn, username, "api", timedelta(hours=hours))


def revoke_tokens(username: str) -> None:
    with connect_owner() as conn:
        conn.execute("DELETE FROM pilot_credential WHERE username = %s", (username,))


def add_commands(commands) -> None:
    parser = commands.add_parser("pilot", help="Provision and operate authenticated Roxi workspaces.")
    actions = parser.add_subparsers(dest="pilot_command", required=True)
    slug = TypeAdapter(Slug).validate_python
    user = TypeAdapter(User).validate_python
    work = actions.add_parser("workspace")
    work.add_argument("workspace", type=slug)
    work.add_argument("name", type=TypeAdapter(DisplayName).validate_python)
    actions.add_parser("account").add_argument("username", type=user)
    binding = actions.add_parser("attach")
    binding.add_argument("workspace", type=slug)
    binding.add_argument("exercise", type=slug)
    for name in ("member", "remove-member", "grant-source", "revoke-source"):
        command = actions.add_parser(name)
        command.add_argument("workspace", type=slug)
        command.add_argument("username", type=user)
        if name == "member":
            command.add_argument("role", choices=sorted(access.ROLES))
            command.add_argument("--all-sources", action="store_true")
        elif name.endswith("source"):
            command.add_argument("exercise", type=slug)
            command.add_argument("source")
    token = actions.add_parser("token")
    token.add_argument("username", type=user)
    token.add_argument("--hours", type=int, default=24)
    actions.add_parser("revoke-tokens").add_argument("username", type=user)
    retention = actions.add_parser("retention")
    retention.add_argument("workspace", type=slug)
    policy = retention.add_mutually_exclusive_group(required=True)
    policy.add_argument("--days", type=TypeAdapter(Days).validate_python)
    policy.add_argument("--disable", action="store_true")
    close = actions.add_parser("close")
    close.add_argument("workspace", type=slug)
    close.add_argument("exercise", type=slug)
    purge = actions.add_parser("purge")
    purge.add_argument("workspace", type=slug)
    purge.add_argument("--apply", action="store_true", help="Delete eligible closed exercises and unreferenced originals.")
    backup = actions.add_parser("backup")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--compose", action="store_true")
    restore = actions.add_parser("restore")
    restore.add_argument("bundle", type=Path)
    restore.add_argument("archive", type=Path)
    restore.add_argument("--compose", action="store_true")


def run(args):
    from . import pilot_backup as backup
    from . import pilot_retention as retention

    match args.pilot_command:
        case "workspace":
            workspace(args.workspace, args.name)
        case "account":
            password = getpass("Password: ")
            if password != getpass("Confirm password: "):
                raise ValueError("The passwords differ.")
            set_password(args.username, password)
        case "attach":
            attach(args.workspace, args.exercise)
        case "member":
            member(args.workspace, args.username, args.role, args.all_sources)
        case "remove-member":
            remove_member(args.workspace, args.username)
        case "grant-source":
            grant_source(args.workspace, args.username, args.exercise, args.source)
        case "revoke-source":
            revoke_source(args.workspace, args.username, args.exercise, args.source)
        case "token":
            return issue_token(args.username, args.hours)
        case "revoke-tokens":
            revoke_tokens(args.username)
        case "retention":
            retention.policy(args.workspace, args.days)
        case "close":
            retention.close(args.workspace, args.exercise)
        case "purge":
            return retention.purge(args.workspace, apply=args.apply)
        case "backup":
            return backup.backup(args.destination, compose=args.compose)
        case "restore":
            target = os.environ.get("ROXI_RESTORE_DATABASE_URL")
            if not target:
                raise ValueError("Set ROXI_RESTORE_DATABASE_URL to the new database.")
            return backup.restore(args.bundle, target, args.archive, compose=args.compose)
    return {"updated": args.pilot_command}
