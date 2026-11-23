import hashlib
import json
from dataclasses import dataclass
from time import sleep
from typing import Annotated

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .extraction import Extractor, Proposal
from .importer import Slug
from .storage import EDITOR_DSN, connect_read

MAX_PAYLOAD = 16_384
BATCH_SIZE = 64
VIEW_SIZE = 60
ROOM_COLUMNS = "exercise_id, room_key, watermark_ms, change_seq"
ITEM_COLUMNS = """id, message_id, version, sent_ms, sender, body, received_at, late, change_seq,
                  classified_at, kind, subject_id, reported_status"""


@dataclass
class LiveError(Exception):
    status: int
    detail: str


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message_id: Slug
    version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    sent_ms: Annotated[int, Field(ge=0, le=31_536_000_000)]
    sender: Slug
    text: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("text")
    @classmethod
    def single_line(cls, value: str) -> str:
        if not value.strip() or value.splitlines() != [value] or "\x00" in value:
            raise ValueError("Use a nonblank, single-line chat message.")
        return value


def connect_editor(autocommit: bool = False):
    return psycopg.connect(EDITOR_DSN, connect_timeout=5, autocommit=autocommit, row_factory=dict_row)


def register_room(exercise_id: str, room_key: str) -> None:
    with connect_editor() as conn:
        if conn.execute("SELECT 1 FROM exercise WHERE id = %s", (exercise_id,)).fetchone() is None:
            raise LiveError(404, "Import the sandbox exercise first.")
        conn.execute(
            "INSERT INTO live_room (exercise_id, room_key) VALUES (%s, %s) ON CONFLICT (exercise_id) DO NOTHING",
            (exercise_id, room_key),
        )
        if room_for(conn, exercise_id)["room_key"] != room_key:
            raise LiveError(409, "This exercise already has a different sandbox room.")


def room_for(conn, exercise_id: str, *, lock: bool = False) -> dict:
    query = f"SELECT {ROOM_COLUMNS} FROM live_room WHERE exercise_id = %s"
    row = conn.execute(query + (" FOR UPDATE" if lock else ""), (exercise_id,)).fetchone()
    if row is None:
        raise LiveError(404, "This exercise has no sandbox room.")
    return row


def registered_room(conn, exercise_id: str) -> str | None:
    row = conn.execute("SELECT room_key FROM live_room WHERE exercise_id = %s", (exercise_id,)).fetchone()
    return row["room_key"] if row else None


def receive(exercise_id: str, room_key: str, message: ChatMessage, original: bytes) -> dict:
    digest = hashlib.sha256(message.model_dump_json().encode()).hexdigest()
    with connect_editor() as conn:
        room = room_for(conn, exercise_id, lock=True)
        if room["room_key"] != room_key:
            raise LiveError(404, "Sandbox room not found.")
        key = (exercise_id, message.message_id)
        existing = conn.execute(
            "SELECT id, payload_sha256 FROM live_chat_version WHERE exercise_id = %s AND message_id = %s AND version = %s",
            (*key, message.version),
        ).fetchone()
        if existing:
            if existing["payload_sha256"] != digest:
                raise LiveError(409, "That message version already exists with different content.")
            return {"id": existing["id"], "duplicate": True}

        previous = conn.execute(
            "SELECT sender, sent_ms, sent_at_utc FROM live_chat_version WHERE exercise_id = %s AND message_id = %s LIMIT 1", key,
        ).fetchone()
        if previous and (previous["sender"], previous["sent_ms"]) != (message.sender, message.sent_ms):
            raise LiveError(409, "An edit must preserve the message sender and original send time.")
        sent_at = previous["sent_at_utc"] if previous else conn.execute(
            "SELECT started_at_utc + %s * interval '1 millisecond' AS sent_at FROM exercise WHERE id = %s",
            (message.sent_ms, exercise_id),
        ).fetchone()["sent_at"]
        received = conn.execute(
            """
            INSERT INTO live_chat_version
                (exercise_id, message_id, version, sent_ms, sent_at_utc, sender, body, original, payload_sha256, late)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (*key, message.version, message.sent_ms, sent_at, message.sender, message.text, original, digest,
             previous is None and message.sent_ms < room["watermark_ms"]),
        ).fetchone()
        if message.sent_ms > room["watermark_ms"]:
            conn.execute("UPDATE live_room SET watermark_ms = %s WHERE exercise_id = %s",
                         (message.sent_ms, exercise_id))
        return {"id": received["id"], "duplicate": False}


def classify_batch(conn) -> int:
    with conn.transaction():
        room = conn.execute(
            """
            SELECT room.exercise_id, room.change_seq, exercise.glossary
            FROM live_room AS room JOIN exercise ON exercise.id = room.exercise_id
            WHERE EXISTS (
                SELECT 1 FROM live_chat_version AS version
                WHERE version.exercise_id = room.exercise_id AND version.change_seq IS NULL
            )
            ORDER BY room.exercise_id
            LIMIT 1 FOR UPDATE OF room SKIP LOCKED
            """
        ).fetchone()
        if room is None:
            return 0
        pending = conn.execute(
            """
            SELECT id, body FROM live_chat_version
            WHERE exercise_id = %s AND change_seq IS NULL ORDER BY id LIMIT %s
            """,
            (room["exercise_id"], BATCH_SIZE),
        ).fetchall()
        extractor = Extractor(room["glossary"])
        updates = []
        for number, item in enumerate(pending, start=room["change_seq"] + 1):
            proposal = extractor.extract(item["body"]) or Proposal("chatter", None, None)
            updates.append((number, proposal.kind, proposal.subject_id, proposal.reported_status, item["id"]))
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                UPDATE live_chat_version SET change_seq = %s, kind = %s, subject_id = %s,
                    reported_status = %s, classified_at = clock_timestamp() WHERE id = %s
                """,
                updates,
            )
        if updates:
            conn.execute("UPDATE live_room SET change_seq = %s WHERE exercise_id = %s",
                         (updates[-1][0], room["exercise_id"]))
        return len(updates)


def work(once: bool = False) -> int:
    with connect_editor(autocommit=True) as conn:
        while True:
            count = classify_batch(conn)
            if once:
                return count
            if not count:
                sleep(0.1)


def current_items(conn, exercise_id: str, limit: int | None = VIEW_SIZE) -> list[dict]:
    return conn.execute(
        f"""
        SELECT * FROM (
            SELECT DISTINCT ON (message_id) {ITEM_COLUMNS}
            FROM live_chat_version WHERE exercise_id = %s AND change_seq IS NOT NULL
            ORDER BY message_id, version DESC
        ) AS current ORDER BY sent_ms DESC, message_id DESC LIMIT %s
        """,
        (exercise_id, limit),
    ).fetchall()


def snapshot(exercise_id: str) -> tuple[int, list[dict]]:
    with connect_read() as conn:
        room = room_for(conn, exercise_id)
        return room["change_seq"], current_items(conn, exercise_id)


def original_evidence(exercise_id: str, item_id: int) -> tuple[dict, dict, list[dict]]:
    with connect_read() as conn:
        exercise = conn.execute("SELECT id, name FROM exercise WHERE id = %s", (exercise_id,)).fetchone()
        record = conn.execute(
            f"SELECT {ITEM_COLUMNS}, original FROM live_chat_version WHERE exercise_id = %s AND id = %s",
            (exercise_id, item_id),
        ).fetchone()
        if record is None:
            raise LiveError(404, "Live chat evidence not found.")
        history = conn.execute(
            "SELECT id, version FROM live_chat_version WHERE exercise_id = %s AND message_id = %s ORDER BY version",
            (exercise_id, record["message_id"]),
        ).fetchall()
        return exercise, record, history


def encode_feed(event: str, seq: int, items: list[dict]) -> str:
    body = json.dumps(items, default=lambda stamp: stamp.isoformat(), separators=(",", ":"))
    return f"id: {seq}\nevent: {event}\ndata: {body}\n\n"
