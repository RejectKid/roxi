from datetime import UTC, datetime

from .storage import OWNER_DSN, archive_lock, archive_path, connect_owner

# Each of these columns points to the shared filesystem archive. Chat originals are in PostgreSQL.
ARCHIVE_REFS = (
    ("pilot_import_archive", "exercise_id", "digest"),
    ("source_file", "exercise_id", "content_sha256"), ("source_file", "exercise_id", "transcript_sha256"),
    ("catalog_capture", "exercise_id", "digest"), ("channel_capture", "exercise_id", "audio_digest"),
    ("history_registry", "exercise_id", "digest"), ("history_layer", "exercise_id", "digest"),
)
DIRECT_CHILDREN = (
    "history_change", "history_change_clock", "history_layer", "history_registry",
    "catalog_attempt", "channel_capture", "review_revision", "review_transcript", "review_event", "review_draft",
    "live_chat_version", "live_room", "exercise_source",
)


def referenced_archive(conn, exercises: list[str] | None = None) -> set[str]:
    parts = []
    for table, scope, digest in ARCHIVE_REFS:
        where = f" AND {scope} = ANY(%(ids)s)" if exercises is not None else ""
        parts.append(f"SELECT {digest} AS digest FROM {table} WHERE {digest} IS NOT NULL{where}")
    return {row["digest"] for row in conn.execute(" UNION ".join(parts), {"ids": exercises}).fetchall()}


def policy(workspace_id: str, days: int | None) -> None:
    with connect_owner() as conn:
        if not conn.execute("UPDATE pilot_workspace SET retention_days = %s WHERE id = %s", (days, workspace_id)).rowcount:
            raise ValueError("Workspace not found.")


def close(workspace_id: str, exercise_id: str) -> None:
    with connect_owner() as conn:
        if not conn.execute(
            "UPDATE pilot_exercise SET closed_at = COALESCE(closed_at, clock_timestamp()) "
            "WHERE workspace_id = %s AND exercise_id = %s", (workspace_id, exercise_id),
        ).rowcount:
            raise ValueError("Exercise not found in this workspace.")


def due(conn, workspace_id: str, now: datetime, *, lock: bool = False) -> list[str]:
    workspace = conn.execute(
        "SELECT retention_days FROM pilot_workspace WHERE id = %s" + (" FOR UPDATE" if lock else ""),
        (workspace_id,),
    ).fetchone()
    if workspace is None:
        raise ValueError("Workspace not found.")
    if workspace["retention_days"] is None:
        return []
    rows = conn.execute(
        "SELECT exercise_id FROM pilot_exercise WHERE workspace_id = %s "
        "AND closed_at + %s * interval '1 day' <= %s ORDER BY exercise_id" + (" FOR UPDATE" if lock else ""),
        (workspace_id, workspace["retention_days"], now),
    ).fetchall()
    return [row["exercise_id"] for row in rows]


def delete_records(conn, ids: list[str]) -> None:
    if not ids:
        return
    digests = referenced_archive(conn, ids)
    with conn.cursor() as cursor:
        cursor.executemany("INSERT INTO pilot_archive_gc VALUES (%s) ON CONFLICT DO NOTHING",
                           ((digest,) for digest in sorted(digests)))
    for table in DIRECT_CHILDREN:
        conn.execute(f"DELETE FROM {table} WHERE exercise_id = ANY(%s)", (ids,))
    conn.execute(
        "DELETE FROM catalog_derivation AS d USING catalog_capture AS c "
        "WHERE d.capture_id = c.id AND c.exercise_id = ANY(%s)", (ids,),
    )
    for table in ("catalog_capture", "catalog_source"):
        conn.execute(f"DELETE FROM {table} WHERE exercise_id = ANY(%s)", (ids,))
    for table in ("source_record", "import_issue"):
        conn.execute(f"DELETE FROM {table} AS item USING source_file AS f "
                     "WHERE item.source_file_id = f.id AND f.exercise_id = ANY(%s)", (ids,))
    conn.execute("DELETE FROM source_file WHERE exercise_id = ANY(%s)", (ids,))
    conn.execute("DELETE FROM exercise WHERE id = ANY(%s)", (ids,))


def collect_archive(conn) -> tuple[int, list[str]]:
    referenced = referenced_archive(conn)
    removed, pending = 0, []
    for row in conn.execute("SELECT digest FROM pilot_archive_gc ORDER BY digest").fetchall():
        digest = row["digest"]
        if digest not in referenced:
            try:
                path = archive_path(digest)
                existed = path.is_file()
                path.unlink(missing_ok=True)
                removed += int(existed)
            except OSError:
                pending.append(digest)
                continue
        conn.execute("DELETE FROM pilot_archive_gc WHERE digest = %s", (digest,))
    return removed, pending


def purge(workspace_id: str, *, apply: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    if not apply:
        with connect_owner() as conn:
            return {"eligible": due(conn, workspace_id, now), "applied": False}
    with archive_lock(shared=False, dsn=OWNER_DSN):
        with connect_owner() as conn:
            ids = due(conn, workspace_id, now, lock=True)
            delete_records(conn, ids)
        with connect_owner() as conn:
            removed, pending = collect_archive(conn)
    return {"eligible": ids, "applied": True, "originals_removed": removed, "archive_cleanup_pending": pending}
