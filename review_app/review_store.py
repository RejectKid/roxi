from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .extraction import Extractor, describe_review, link_options, subjects, timed_subjects
from .live_chat import registered_room
from .reported_times import time_replaces
from .storage import EDITOR_DSN, connect_read


@dataclass
class ReviewError(Exception):
    status: int
    detail: str


def exercise_row(conn, exercise_id: str) -> dict:
    row = conn.execute(
        """
        SELECT e.id, e.name, e.glossary,
               ARRAY(SELECT x.source_file_id FROM exercise_source AS x
                     WHERE x.exercise_id = e.id ORDER BY x.position) AS source_file_ids
        FROM exercise AS e WHERE e.id = %s
        """,
        (exercise_id,),
    ).fetchone()
    if row is None:
        raise ReviewError(404, "Exercise not found.")
    return row


def get_draft(conn, exercise_id: str, *, lock: bool = False) -> dict | None:
    query = "SELECT * FROM review_draft WHERE exercise_id = %s"
    return conn.execute(query + (" FOR UPDATE" if lock else ""), (exercise_id,)).fetchone()


def require_version(draft: dict | None, expected: int | None) -> None:
    if draft is None or draft["version"] != expected:
        raise ReviewError(409, "This draft has changed. Reload to see the latest version and reapply your edit.")


def record_rows(conn, exercise_id: str, source_ids: list[int]) -> list[dict]:
    return conn.execute(
        """
        SELECT r.*, s.kind AS source_kind, s.original_name, s.source_key, t.body AS edited_body
        FROM source_record AS r JOIN source_file AS s ON s.id = r.source_file_id
        LEFT JOIN review_transcript AS t
          ON t.exercise_id = s.exercise_id AND t.source_file_id = r.source_file_id AND t.ordinal = r.ordinal
        WHERE s.exercise_id = %s AND r.source_file_id = ANY(%s)
        ORDER BY r.capture_offset_ms NULLS LAST, array_position(%s::bigint[], r.source_file_id), r.ordinal
        """,
        (exercise_id, source_ids, source_ids),
    ).fetchall()


def event_rows(conn, draft: dict) -> list[dict]:
    exercise_id = draft["exercise_id"]
    records = record_rows(conn, exercise_id, draft["source_file_ids"])
    events = conn.execute(
        "SELECT * FROM review_event WHERE exercise_id = %s AND source_file_id = ANY(%s)",
        (exercise_id, draft["source_file_ids"]),
    ).fetchall()
    by_source = {(event["source_file_id"], event["ordinal"]): event for event in events}
    ordered = []
    for record in records:
        event = by_source.get((record["source_file_id"], record["ordinal"]))
        if event is not None:
            event.update({key: record[key] for key in ("capture_offset_ms", "audio_start_ms", "audio_end_ms",
                                                       "source_kind", "original_name", "cue_id", "speaker")})
            event["body"] = record["edited_body"] or record["body"]
            ordered.append(event)
    return ordered


def bump_version(conn, exercise_id: str) -> int:
    return conn.execute(
        "UPDATE review_draft SET version = version + 1 WHERE exercise_id = %s RETURNING version",
        (exercise_id,),
    ).fetchone()["version"]


def start_or_refresh(exercise_id: str, version: int | None) -> dict:
    with psycopg.connect(EDITOR_DSN, connect_timeout=5, row_factory=dict_row) as conn:
        exercise = exercise_row(conn, exercise_id)
        created = conn.execute(
            """
            INSERT INTO review_draft (exercise_id, source_file_ids, glossary)
            VALUES (%s, %s, %s) ON CONFLICT (exercise_id) DO NOTHING RETURNING exercise_id
            """,
            (exercise_id, exercise["source_file_ids"], Jsonb(exercise["glossary"])),
        ).fetchone()
        draft = get_draft(conn, exercise_id, lock=True)
        if not created or version is not None:
            require_version(draft, version)

        # Resolve one committed input set after obtaining the draft lock.
        exercise = exercise_row(conn, exercise_id)
        source_ids = exercise["source_file_ids"]
        glossary = exercise["glossary"]
        changed = source_ids != draft["source_file_ids"] or glossary != draft["glossary"]
        if changed:
            conn.execute(
                "UPDATE review_draft SET source_file_ids = %s, glossary = %s WHERE exercise_id = %s",
                (source_ids, Jsonb(glossary), exercise_id),
            )
        extractor = Extractor(glossary, elapsed_times=True)
        proposals = []
        for record in record_rows(conn, exercise_id, source_ids):
            proposal = extractor.extract(record["edited_body"] or record["body"])
            if proposal is not None:
                proposals.append((exercise_id, record["source_file_id"], record["ordinal"],
                                  proposal.kind, proposal.subject_id, proposal.reported_status, proposal.reported_time_seconds))
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO review_event (exercise_id, source_file_id, ordinal, kind, subject_id,
                                          reported_status, reported_time_seconds)
                VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (exercise_id, source_file_id, ordinal)
                DO UPDATE SET kind = EXCLUDED.kind, subject_id = EXCLUDED.subject_id, reported_status = NULL,
                              reported_time_seconds = EXCLUDED.reported_time_seconds
                WHERE review_event.state = 'proposed' AND review_event.kind = 'acknowledgment'
                  AND review_event.target_id IS NULL AND EXCLUDED.reported_time_seconds IS NOT NULL
                """,
                proposals,
            )
            added = max(cursor.rowcount, 0)
        if changed:
            # Preserve original records. Reopen accepted links whose evidence or dependency left this draft.
            conn.execute(
                """
                WITH RECURSIVE affected(id) AS (
                    SELECT e.id FROM review_event AS e JOIN review_event AS target ON target.id = e.target_id
                    WHERE e.exercise_id = %s AND e.source_file_id = ANY(%s)
                      AND NOT (target.source_file_id = ANY(%s))
                    UNION
                    SELECT e.id FROM review_event AS e JOIN affected AS upstream ON e.target_id = upstream.id
                    WHERE e.exercise_id = %s AND e.source_file_id = ANY(%s)
                )
                UPDATE review_event AS e SET state = CASE WHEN e.state = 'rejected' THEN e.state ELSE 'proposed' END,
                    target_id = CASE WHEN target.source_file_id = ANY(%s) THEN e.target_id ELSE NULL END
                FROM review_event AS target
                WHERE e.target_id = target.id AND e.id IN (SELECT id FROM affected)
                """,
                (exercise_id, source_ids, source_ids, exercise_id, source_ids, source_ids),
            )
        return {"version": bump_version(conn, exercise_id) if changed or added else draft["version"], "added": added}


def get_review(exercise_id: str) -> dict:
    with connect_read() as conn:
        exercise = exercise_row(conn, exercise_id)
        draft = get_draft(conn, exercise_id)
        events = event_rows(conn, draft) if draft else []
        latest = conn.execute(
            "SELECT number, draft_version FROM review_revision WHERE exercise_id = %s ORDER BY number DESC LIMIT 1",
            (exercise_id,),
        ).fetchone()
        room = registered_room(conn, exercise_id)
    describe_review(events)
    return {"exercise": exercise, "draft": draft, "events": events, "latest": latest, "live_room": room,
            "live_subjects": subjects(exercise["glossary"]) if room else {},
            "subjects": subjects(draft["glossary"]) if draft else {},
            "inputs_changed": bool(draft and (exercise["source_file_ids"] != draft["source_file_ids"]
                                               or exercise["glossary"] != draft["glossary"]))}


def save_event(exercise_id: str, event_id: int, version: int, subject_id: str | None,
               reported_status: str | None, state: str, target_id: int | None,
               reported_time_seconds: int | None = None) -> int:
    with psycopg.connect(EDITOR_DSN, connect_timeout=5, row_factory=dict_row) as conn:
        draft = get_draft(conn, exercise_id, lock=True)
        require_version(draft, version)
        events = event_rows(conn, draft)
        event = next((item for item in events if item["id"] == event_id), None)
        if event is None:
            raise ReviewError(404, "Event not found in this draft.")
        if subject_id is not None and subject_id not in subjects(draft["glossary"]):
            raise ReviewError(422, "Choose an exercise subject or leave it unresolved.")
        if event["reported_time_seconds"] is not None and reported_time_seconds is None:
            reported_time_seconds = event["reported_time_seconds"]
        if reported_time_seconds is not None and (
            event["reported_time_seconds"] is None or reported_status is not None
            or subject_id not in timed_subjects(draft["glossary"])
        ):
            raise ReviewError(422, "Use a timed report subject and an elapsed time.")
        if event["kind"] == "status_report" and not reported_status:
            raise ReviewError(422, "Choose the reported status.")
        if event["kind"] == "acknowledgment" and reported_status is not None:
            raise ReviewError(422, "An acknowledgment records receipt.")
        if event["kind"] == "correction" and state == "accepted" and subject_id is None and reported_status is None:
            raise ReviewError(422, "Provide the replacement subject or status before accepting the correction.")
        revised = {**event, "subject_id": subject_id, "reported_time_seconds": reported_time_seconds}
        if reported_time_seconds is not None and event["kind"] == "correction" and state == "accepted" and target_id is None:
            raise ReviewError(422, "Choose the earlier time report before accepting the correction.")
        if target_id is not None:
            target = next((item for item in link_options(revised, events) if item["id"] == target_id), None)
            if target is None:
                raise ReviewError(422, "Choose an earlier eligible report or correction as the link target.")
            if state == "accepted" and target["state"] != "accepted":
                raise ReviewError(422, "Accept the linked event first, or save this event without a link.")
        if state != "accepted" and any(item["state"] == "accepted" and item["target_id"] == event_id for item in events):
            raise ReviewError(422, "Update the accepted links to this event before changing its review decision.")
        if reported_time_seconds is not None and any(
            item["state"] == "accepted" and item["kind"] == "correction" and item["target_id"] == event_id
            and not time_replaces(item, revised) for item in events
        ):
            raise ReviewError(422, "Update the accepted time correction before changing the linked report.")
        if (event["subject_id"], event["reported_status"], event["state"], event["target_id"], event["reported_time_seconds"]) == (
            subject_id, reported_status, state, target_id, reported_time_seconds
        ):
            return draft["version"]
        conn.execute(
            "UPDATE review_event SET subject_id = %s, reported_status = %s, state = %s, target_id = %s, "
            "reported_time_seconds = %s WHERE id = %s",
            (subject_id, reported_status, state, target_id, reported_time_seconds, event_id),
        )
        return bump_version(conn, exercise_id)


def revision_row(conn, exercise_id: str, number: int) -> dict:
    row = conn.execute(
        "SELECT * FROM review_revision WHERE exercise_id = %s AND number = %s", (exercise_id, number)
    ).fetchone()
    if row is None:
        raise ReviewError(404, "Published revision not found.")
    return row


def publish(exercise_id: str, version: int) -> int:
    with psycopg.connect(EDITOR_DSN, connect_timeout=5, row_factory=dict_row) as conn:
        draft = get_draft(conn, exercise_id, lock=True)
        existing = conn.execute(
            "SELECT number FROM review_revision WHERE exercise_id = %s AND draft_version = %s",
            (exercise_id, version),
        ).fetchone()
        if existing:
            return existing["number"]
        require_version(draft, version)
        exercise = exercise_row(conn, exercise_id)
        events = event_rows(conn, draft)
        describe_review(events)
        transcripts = conn.execute(
            """
            SELECT source_file_id, ordinal, body FROM review_transcript
            WHERE exercise_id = %s AND source_file_id = ANY(%s)
            ORDER BY source_file_id, ordinal
            """,
            (exercise_id, draft["source_file_ids"]),
        ).fetchall()
        snapshot = {
            "exercise_name": exercise["name"],
            "source_file_ids": draft["source_file_ids"],
            "glossary": draft["glossary"],
            "events": [{key: value for key, value in event.items() if key != "options"} for event in events],
            "transcripts": transcripts,
        }
        number = conn.execute(
            "SELECT COALESCE(max(number), 0) + 1 AS number FROM review_revision WHERE exercise_id = %s",
            (exercise_id,),
        ).fetchone()["number"]
        conn.execute(
            "INSERT INTO review_revision (exercise_id, number, draft_version, snapshot) VALUES (%s, %s, %s, %s)",
            (exercise_id, number, version, Jsonb(snapshot)),
        )
        return number


def get_revisions(exercise_id: str, number: int | None = None) -> dict:
    with connect_read() as conn:
        exercise = exercise_row(conn, exercise_id)
        revisions = conn.execute(
            """
            SELECT number, published_at, jsonb_array_length(snapshot->'events') AS event_count
            FROM review_revision WHERE exercise_id = %s ORDER BY number DESC
            """,
            (exercise_id,),
        ).fetchall()
        selected = revision_row(conn, exercise_id, number) if number is not None else None
    snapshot = selected["snapshot"] if selected else {}
    if selected:
        exercise["name"] = snapshot["exercise_name"]
    return {"exercise": exercise, "revisions": revisions, "selected": selected,
            "events": snapshot.get("events", []), "transcripts": snapshot.get("transcripts", []),
            "subjects": subjects(snapshot.get("glossary", {}))}


def evidence_record(conn, exercise_id: str, source_id: int, ordinal: int) -> dict:
    record = conn.execute(
        """
        SELECT r.*, s.kind AS source_kind, s.original_name, s.content_sha256, s.source_key
        FROM source_record AS r JOIN source_file AS s ON s.id = r.source_file_id
        WHERE s.exercise_id = %s AND r.source_file_id = %s AND r.ordinal = %s
        """,
        (exercise_id, source_id, ordinal),
    ).fetchone()
    if record is None:
        raise ReviewError(404, "Source evidence not found.")
    return record


def get_evidence(exercise_id: str, source_id: int, ordinal: int, number: int | None) -> dict:
    with connect_read() as conn:
        exercise = exercise_row(conn, exercise_id)
        record = evidence_record(conn, exercise_id, source_id, ordinal)
        if number is not None:
            snapshot = revision_row(conn, exercise_id, number)["snapshot"]
            if source_id not in snapshot["source_file_ids"]:
                raise ReviewError(404, "This source is outside the published revision.")
            exercise["name"] = snapshot["exercise_name"]
            edited = next((item for item in snapshot["transcripts"] if item["source_file_id"] == source_id
                           and item["ordinal"] == ordinal), None)
            version = None
        else:
            draft = get_draft(conn, exercise_id)
            version = draft["version"] if draft and source_id in draft["source_file_ids"] else None
            edited = conn.execute(
                "SELECT body FROM review_transcript WHERE exercise_id = %s AND source_file_id = %s AND ordinal = %s",
                (exercise_id, source_id, ordinal),
            ).fetchone() if version is not None else None
    return {"exercise": exercise, "record": record, "number": number, "version": version,
            "effective_body": edited["body"] if edited else record["body"]}


def save_transcript(exercise_id: str, source_id: int, ordinal: int, version: int, body: str) -> int:
    with psycopg.connect(EDITOR_DSN, connect_timeout=5, row_factory=dict_row) as conn:
        draft = get_draft(conn, exercise_id, lock=True)
        require_version(draft, version)
        record = evidence_record(conn, exercise_id, source_id, ordinal)
        if source_id not in draft["source_file_ids"] or record["source_kind"] != "audio":
            raise ReviewError(422, "Choose a radio cue in the current draft.")
        current = conn.execute(
            "SELECT body FROM review_transcript WHERE exercise_id = %s AND source_file_id = %s AND ordinal = %s",
            (exercise_id, source_id, ordinal),
        ).fetchone()
        if body == (current["body"] if current else record["body"]):
            return draft["version"]
        conn.execute(
            """
            INSERT INTO review_transcript (exercise_id, source_file_id, ordinal, body) VALUES (%s, %s, %s, %s)
            ON CONFLICT (exercise_id, source_file_id, ordinal) DO UPDATE SET body = EXCLUDED.body
            """,
            (exercise_id, source_id, ordinal, body),
        )
        return bump_version(conn, exercise_id)
