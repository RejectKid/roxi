import base64
import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel

from . import query_models as model
from .storage import archive_path, connect_read

# Effective transcript text belongs to the selected revision, including when the draft has moved on.
RECORD_SOURCE = """
FROM source_record AS r JOIN source_file AS s ON s.id = r.source_file_id
LEFT JOIN jsonb_to_recordset(%s::jsonb) AS t(source_file_id bigint, ordinal integer, body text)
    ON t.source_file_id = r.source_file_id AND t.ordinal = r.ordinal
"""
RECORD_COLUMNS = """
r.*, s.source_key, s.kind AS source_kind, s.original_name, s.content_sha256, s.byte_size,
COALESCE(t.body, r.body) AS excerpt
"""
RECORD_ORDER = "(r.capture_offset_ms IS NULL, COALESCE(r.capture_offset_ms, 0), r.source_file_id, r.ordinal)"


@dataclass
class QueryError(Exception):
    status: int
    code: str
    message: str
    retryable: bool = False

    def result(self) -> model.ErrorResult:
        return model.ErrorResult(error=model.ErrorInfo(
            code=self.code, message=self.message, retryable=self.retryable, request_id=str(uuid4())
        ))


def invalid(message: str = "The request arguments are invalid.") -> QueryError:
    return QueryError(400, "invalid_argument", message)


def not_found(message: str = "Published revision not found.") -> QueryError:
    return QueryError(404, "not_found", message)


def unavailable() -> QueryError:
    return QueryError(503, "temporarily_unavailable", "The local PostgreSQL database is unavailable.", True)


def check_scope(exercise_id: str) -> None:
    setting = os.environ.get("EXERCISE_REVIEW_API_EXERCISES")
    if setting is not None and exercise_id not in {part.strip() for part in setting.split(",")}:
        raise not_found()


def revision_row(conn, exercise_id: str, revision_id: int | None = None) -> dict:
    check_scope(exercise_id)
    if revision_id is None:
        row = conn.execute(
            "SELECT * FROM review_revision WHERE exercise_id = %s ORDER BY number DESC LIMIT 1",
            (exercise_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM review_revision WHERE exercise_id = %s AND number = %s",
            (exercise_id, revision_id),
        ).fetchone()
    if row is None:
        raise not_found()
    return row


def coverage(conn, revision: dict, excluded: int = 0) -> model.Coverage:
    sources = conn.execute(
        """
        SELECT s.source_key, s.kind, s.exercise_offset_ms,
               (SELECT count(*) FROM import_issue AS i WHERE i.source_file_id = s.id) AS issues
        FROM source_file AS s WHERE s.exercise_id = %s AND s.id = ANY(%s)
        ORDER BY s.source_key
        """,
        (revision["exercise_id"], revision["snapshot"]["source_file_ids"]),
    ).fetchall()
    known_gaps = []
    unaligned = 0
    for source in sources:
        if source["kind"] == "audio" and source["exercise_offset_ms"] is None:
            known_gaps.append(f"{source['source_key']}: exercise clock anchor missing")
            unaligned += 1
        count = source["issues"]
        if count:
            known_gaps.append(f"{source['source_key']}: {count} malformed chat {'line' if count == 1 else 'lines'} skipped")
    return model.Coverage(aligned_sources=len(sources) - unaligned, unaligned_sources=unaligned,
                          known_gaps=known_gaps, unknown_time_excluded=excluded)


def get_exercise_revision(request: model.ExerciseRequest) -> model.RevisionResult:
    with connect_read() as conn:
        row = revision_row(conn, request.exercise_id)
        return model.RevisionResult(exercise_id=request.exercise_id, revision_id=row["number"],
                                    exercise_name=row["snapshot"]["exercise_name"], published_at=row["published_at"],
                                    coverage=coverage(conn, row))


def get_glossary(request: model.RevisionRequest) -> dict:
    with connect_read() as conn:
        row = revision_row(conn, request.exercise_id, request.revision_id)
        return {"exercise_id": request.exercise_id, "revision_id": request.revision_id,
                "glossary": row["snapshot"]["glossary"]}


def check_window(filters: model.PageFilters) -> None:
    if filters.start_ms is not None and filters.end_ms is not None and filters.start_ms >= filters.end_ms:
        raise invalid("start_ms must be less than end_ms.")


def filter_fingerprint(operation: str, request: BaseModel) -> str:
    query = request.model_dump(mode="json", exclude={"cursor", "limit"})
    if isinstance(request, model.EventRequest):
        query["event_types"] = sorted(set(query["event_types"]))
    return hashlib.sha256(json.dumps([operation, query], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def encode_cursor(fingerprint: str, key: tuple[bool, int, int, int]) -> str:
    payload = json.dumps([fingerprint, key], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str | None, fingerprint: str) -> tuple[bool, int, int, int] | None:
    if cursor is None:
        return None
    error = invalid("The cursor does not match these revision and query filters.")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
    except ValueError:
        raise error from None
    if not isinstance(value, list) or len(value) != 2 or value[0] != fingerprint:
        raise error
    key = value[1]
    if (
        not isinstance(key, list) or len(key) != 4 or type(key[0]) is not bool
        or any(type(item) is not int or not 0 <= item <= model.MAX_BIGINT for item in key[1:])
    ):
        raise error
    return tuple(key)


def order_key(capture: int | None, first: int, second: int = 0) -> tuple[bool, int, int, int]:
    return (capture is None, capture or 0, first, second)


def timing_quality(capture: int | None) -> str:
    return "unknown" if capture is None else "exact"


def evidence_id(source_file_id: int, ordinal: int, kind: str, body: str) -> str:
    prefix = f"s{source_file_id}-r{ordinal}"
    return f"{prefix}-t{hashlib.sha256(body.encode()).hexdigest()}" if kind == "audio" else prefix


def record_item(row: dict) -> model.RecordItem:
    source_id = row["source_file_id"]
    ordinal = row["ordinal"]
    kind = row["source_kind"]
    capture = row["capture_offset_ms"]
    if kind == "audio":
        locator = model.AudioInterval(start_ms=row["audio_start_ms"], end_ms=row["audio_end_ms"],
                                      cue_id=row["cue_id"])
    else:
        locator = model.ChatLines(line_start=ordinal, line_end=ordinal)
    return model.RecordItem(evidence_id=evidence_id(source_id, ordinal, kind, row["excerpt"]),
                            source_file_id=source_id, channel=row["source_key"], source_kind=kind,
                            original_name=row["original_name"], speaker=row["speaker"], capture_offset_ms=capture,
                            timing_quality=timing_quality(capture), excerpt=row["excerpt"], locator=locator)


def search_records(request: model.RecordRequest) -> model.RecordPage:
    check_window(request)
    fingerprint = filter_fingerprint("records", request)
    cursor = decode_cursor(request.cursor, fingerprint)
    with connect_read() as conn:
        row = revision_row(conn, request.exercise_id, request.revision_id)
        snapshot = row["snapshot"]
        clauses = ["s.exercise_id = %s", "r.source_file_id = ANY(%s)"]
        params = [Jsonb(snapshot["transcripts"]), request.exercise_id, snapshot["source_file_ids"]]
        if request.channel:
            clauses.append("s.source_key = %s")
            params.append(request.channel)
        if request.text:
            clauses.append("strpos(lower(COALESCE(t.body, r.body)), lower(%s)) > 0")
            params.append(request.text)

        excluded = 0
        if request.start_ms is not None or request.end_ms is not None:
            where = " AND ".join(clauses)
            excluded = conn.execute(
                f"SELECT count(*) AS count {RECORD_SOURCE} WHERE {where} AND r.capture_offset_ms IS NULL",
                params,
            ).fetchone()["count"]
        if request.start_ms is not None:
            clauses.append("r.capture_offset_ms >= %s")
            params.append(request.start_ms)
        if request.end_ms is not None:
            clauses.append("r.capture_offset_ms < %s")
            params.append(request.end_ms)
        if cursor is not None:
            clauses.append(f"{RECORD_ORDER} > (%s, %s, %s, %s)")
            params.extend(cursor)
        where = " AND ".join(clauses)
        hits = conn.execute(
            f"""SELECT {RECORD_COLUMNS} {RECORD_SOURCE} WHERE {where}
                ORDER BY r.capture_offset_ms IS NULL, COALESCE(r.capture_offset_ms, 0), r.source_file_id, r.ordinal
                LIMIT %s""",
            [*params, request.limit + 1],
        ).fetchall()
        source_coverage = coverage(conn, row, excluded)

    page = hits[:request.limit]
    next_cursor = None
    if len(hits) > request.limit:
        last = page[-1]
        next_cursor = encode_cursor(fingerprint, order_key(last["capture_offset_ms"], last["source_file_id"], last["ordinal"]))
    return model.RecordPage(exercise_id=request.exercise_id, revision_id=request.revision_id,
                            items=[record_item(hit) for hit in page], coverage=source_coverage, next_cursor=next_cursor)


def event_evidence(event: dict) -> str:
    return evidence_id(event["source_file_id"], event["ordinal"], event["source_kind"], event["body"])


def visible_at(event: dict, cutoff: int | None) -> bool:
    capture = event["capture_offset_ms"]
    return cutoff is None or (capture is not None and capture <= cutoff)


def published_links(events: list[dict], cutoff: int | None, review_state: str) -> dict[int, list[model.Relationship]]:
    eligible = {event["id"]: event for event in events if visible_at(event, cutoff)}
    edges = defaultdict(list)
    for event in eligible.values():
        target = eligible.get(event["target_id"])
        if target is None or (review_state != "all" and event["state"] != review_state):
            continue
        common = {
            "type": "acknowledges" if event["kind"] == "acknowledgment" else "corrects",
            "review_state": event["state"],
            "evidence_ids": [event_evidence(target), event_evidence(event)],
        }
        for origin, other, direction in ((event, target, "outgoing"), (target, event, "incoming")):
            edges[origin["id"]].append(model.Relationship(
                **common, direction=direction, event_id=other["id"], event_type=other["kind"], subject_id=other["subject_id"]
            ))
    return edges


def event_item(event: dict, links: list[model.Relationship], conflicting: list[int], candidates: list[int]) -> model.PublishedEvent:
    capture = event["capture_offset_ms"]
    common = {
        "event_id": event["id"], "subject_id": event["subject_id"], "capture_offset_ms": capture,
        "timing_quality": timing_quality(capture), "review_state": event["state"],
        "evidence_ids": [event_evidence(event)], "relationships": links, "conflicting_event_ids": conflicting,
        "candidate_event_ids": candidates,
    }
    if event["kind"] == "status_report":
        return model.ReportEvent(**common, payload=model.ReportPayload(reported_status=event["reported_status"]))
    if event["kind"] == "acknowledgment":
        return model.AcknowledgmentEvent(**common, payload=model.AcknowledgmentPayload(text=event["body"]))
    return model.CorrectionEvent(**common, payload=model.CorrectionPayload(
        replacement_subject_id=event["subject_id"], replacement_reported_status=event["reported_status"]
    ))


def list_events(request: model.EventRequest) -> model.EventPage:
    check_window(request)
    fingerprint = filter_fingerprint("events", request)
    cursor = decode_cursor(request.cursor, fingerprint)
    with connect_read() as conn:
        revision = revision_row(conn, request.exercise_id, request.revision_id)
        events = revision["snapshot"]["events"]
        matching = [
            event for event in events
            if (not request.subject_id or event["subject_id"] == request.subject_id)
            and (not request.event_types or event["kind"] in request.event_types)
            and (request.review_state == "all" or event["state"] == request.review_state)
        ]
        has_time_filter = any(value is not None for value in (request.start_ms, request.end_ms, request.as_of_ms))
        excluded = sum(event["capture_offset_ms"] is None for event in matching) if has_time_filter else 0
        source_coverage = coverage(conn, revision, excluded)

    def key(event):
        return order_key(event["capture_offset_ms"], event["id"])

    hits = []
    for event in matching:
        capture = event["capture_offset_ms"]
        if (has_time_filter and capture is None) or not visible_at(event, request.as_of_ms):
            continue
        if request.start_ms is not None and capture < request.start_ms:
            continue
        if request.end_ms is not None and capture >= request.end_ms:
            continue
        if cursor is None or key(event) > cursor:
            hits.append(event)
    hits.sort(key=key)
    page = hits[:request.limit]
    links = published_links(events, request.as_of_ms, request.review_state)
    eligible_ids = {event["id"] for event in events if visible_at(event, request.as_of_ms)
                    and (request.review_state == "all" or event["state"] == request.review_state)}
    items = [event_item(event, links.get(event["id"], []), [
        other for other in event["conflicts"] if other in eligible_ids
    ], [
        other for other in event.get("candidates", []) if other in eligible_ids and event["target_id"] is None
    ]) for event in page]
    next_cursor = encode_cursor(fingerprint, key(page[-1])) if len(hits) > request.limit else None
    return model.EventPage(exercise_id=request.exercise_id, revision_id=request.revision_id,
                           items=items, coverage=source_coverage, next_cursor=next_cursor)


def get_evidence(request: model.EvidenceRequest) -> model.EvidenceResult:
    source_part, record_part, *_ = request.evidence_id.split("-")
    source_id, ordinal = int(source_part[1:]), int(record_part[1:])
    with connect_read() as conn:
        found = revision_row(conn, request.exercise_id, request.revision_id)
        snapshot = found["snapshot"]
        if source_id not in snapshot["source_file_ids"] or ordinal > model.MAX_INT:
            raise not_found("Evidence is outside the published revision or does not exist.")
        row = conn.execute(
            f"""SELECT {RECORD_COLUMNS} {RECORD_SOURCE}
                WHERE s.exercise_id = %s AND s.id = %s AND r.ordinal = %s""",
            (Jsonb(snapshot["transcripts"]), request.exercise_id, source_id, ordinal),
        ).fetchone()
    if row is None:
        raise not_found("Evidence is outside the published revision or does not exist.")
    item = record_item(row)
    if item.evidence_id != request.evidence_id:
        raise not_found("Evidence is outside the published revision or does not exist.")
    if not archive_path(row["content_sha256"]).is_file():
        raise QueryError(410, "source_unavailable", "The original source is unavailable in the local archive.")

    original = (f"/v1/exercises/{request.exercise_id}/revisions/{request.revision_id}"
                f"/evidence/{request.evidence_id}/original")
    playback = None
    if isinstance(item.locator, model.AudioInterval):
        playback = f"{original}#t={item.locator.start_ms / 1000},{item.locator.end_ms / 1000}"
    return model.EvidenceResult(
        **item.model_dump(), exercise_id=request.exercise_id, revision_id=request.revision_id,
        original_excerpt=row["body"], original_text=row["original_text"], source_sha256=row["content_sha256"],
        source_byte_size=row["byte_size"], original_path=original, playback_path=playback,
    )
