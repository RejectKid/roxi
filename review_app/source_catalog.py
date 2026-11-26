import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from .importer import Slug
from .live_chat import connect_editor
from .source_adapters import ADAPTERS, CHANNEL_ADAPTERS, CONTRACT_VERSION, MAX_PAYLOAD_BYTES, Label
from .storage import archive_capture, archive_path, archive_writer, connect_read

ITEM_LIMIT = 50


@dataclass
class CatalogError(Exception):
    status: int
    detail: str


EntityId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,96}$")]
Station = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{3,8}$")]


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: Slug
    label: Label
    adapter_key: str
    stale_after_seconds: Annotated[int, Field(gt=0, le=31_536_000, strict=True)]
    expected_ids: Annotated[list[EntityId], Field(max_length=1000)] = Field(default_factory=list)
    nws_station: Station | None = None

    @model_validator(mode="after")
    def validate_source(self):
        if self.adapter_key not in ADAPTERS:
            raise ValueError(f"Choose an adapter from {', '.join(ADAPTERS)}.")
        if self.nws_station and self.adapter_key != "nws-observations":
            raise ValueError("An NWS station is only supported by the weather adapter.")
        if len(set(self.expected_ids)) != len(self.expected_ids):
            raise ValueError("Coverage identifiers must be unique.")
        return self


@dataclass(frozen=True)
class Receipt:
    source_key: str
    capture_id: int | None
    parser_version: int | None
    state: str
    records: int = 0
    issues: int = 0
    reused_derivation: bool = False
    failure: str | None = None


def registered(conn, exercise_id, source_key, *, lock=False):
    row = conn.execute(
        "SELECT * FROM catalog_source WHERE exercise_id = %s AND source_key = %s" + (" FOR UPDATE" if lock else ""),
        (exercise_id, source_key),
    ).fetchone()
    if not row:
        raise CatalogError(404, "Source is not registered for this exercise.")
    return row


def register(exercise_id: str, config_file: Path) -> int:
    configs = TypeAdapter(list[SourceConfig]).validate_json(config_file.read_bytes())
    if not configs or len({item.source_key for item in configs}) != len(configs):
        raise ValueError("Supply at least one source and use distinct source keys.")
    with connect_editor() as conn:
        if not conn.execute("SELECT 1 FROM exercise WHERE id = %s", (exercise_id,)).fetchone():
            raise CatalogError(404, "Import the exercise before registering sources.")
        for config in configs:
            expected = config.model_dump()
            conn.execute(
                """
                INSERT INTO catalog_source
                    (exercise_id, source_key, label, adapter_key, stale_after_seconds, expected_ids, nws_station)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (exercise_id, source_key) DO NOTHING
                """,
                (exercise_id, config.source_key, config.label, config.adapter_key, config.stale_after_seconds,
                 config.expected_ids, config.nws_station),
            )
            found = registered(conn, exercise_id, config.source_key)
            if any(found[key] != value for key, value in expected.items()):
                raise CatalogError(409, f"Source {config.source_key} already has a different configuration.")
    return len(configs)


def derive(conn, capture_id, adapter, version, payload) -> dict:
    row = conn.execute(
        """
        SELECT state, jsonb_array_length(records) AS records, jsonb_array_length(issues) AS issues
        FROM catalog_derivation WHERE capture_id = %s AND parser_version = %s
        """,
        (capture_id, version),
    ).fetchone()
    if row:
        return {**row, "reused_derivation": True}
    return save_derivation(conn, capture_id, adapter, version, adapter.parse(payload, version))


def save_derivation(conn, capture_id, adapter, version, parsed) -> dict:
    records = []
    for item in parsed.records:
        row = asdict(item)
        row["observed_at"] = item.observed_at.isoformat()
        records.append(row)
    issues = [asdict(item) for item in parsed.issues]
    observed = max((item.observed_at for item in parsed.records), default=None)
    conn.execute(
        """
        INSERT INTO catalog_derivation
            (capture_id, parser_version, contract_version, input_schema, state, observed_at, records, issues)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (capture_id, version, CONTRACT_VERSION, adapter.input_schema, parsed.state, observed, Jsonb(records), Jsonb(issues)),
    )
    return {"state": parsed.state, "records": len(records), "issues": len(issues)}


@archive_writer
def ingest(exercise_id: str, source_key: str, name: str, data: bytes, version: int | None, channel: str) -> Receipt:
    with connect_editor() as conn:
        source = registered(conn, exercise_id, source_key, lock=True)
        adapter = ADAPTERS[source["adapter_key"]]
        if source["adapter_key"] in CHANNEL_ADAPTERS:
            raise CatalogError(422, "Send chat or radio through the versioned channel capture endpoint.")
        selected = adapter.select_version(version)
        original = archive_capture(data)
        key = (exercise_id, source_key, original.sha256)
        conn.execute(
            """
            INSERT INTO catalog_capture (exercise_id, source_key, digest, byte_size, original_name)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (exercise_id, source_key, digest) DO NOTHING
            """,
            (*key, original.byte_size, name),
        )
        capture_id = conn.execute(
            "SELECT id FROM catalog_capture WHERE exercise_id = %s AND source_key = %s AND digest = %s",
            key,
        ).fetchone()["id"]
        fields = derive(conn, capture_id, adapter, selected, data)
        conn.execute(
            """
            INSERT INTO catalog_attempt (exercise_id, source_key, channel, capture_id, parser_version)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (exercise_id, source_key, channel, capture_id, selected),
        )
    return Receipt(source_key, capture_id, selected, **fields)


def record_failure(exercise_id, source_key, channel, detail) -> Receipt:
    with connect_editor() as conn:
        registered(conn, exercise_id, source_key, lock=True)
        conn.execute(
            "INSERT INTO catalog_attempt (exercise_id, source_key, channel, failure) VALUES (%s, %s, %s, %s)",
            (exercise_id, source_key, channel, detail),
        )
    return Receipt(source_key, None, None, "unavailable", failure=detail)


def read_bounded(stream) -> bytes:
    payload = stream.read(MAX_PAYLOAD_BYTES + 1)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("Capture exceeds the 2 MiB sandbox limit.")
    return payload


def ingest_file(exercise_id: str, source_key: str, path: Path, version: int | None = None) -> Receipt:
    with connect_read() as conn:
        source = registered(conn, exercise_id, source_key)
        selected = ADAPTERS[source["adapter_key"]].select_version(version)
    try:
        with path.open("rb") as stream:
            payload = read_bounded(stream)
    except ValueError as error:
        return record_failure(exercise_id, source_key, "file", str(error))
    except OSError:
        return record_failure(exercise_id, source_key, "file", "Could not read the source file.")
    return ingest(exercise_id, source_key, path.name, payload, selected, "file")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_weather(station: str) -> bytes:
    request = Request(f"https://api.weather.gov/stations/{station}/observations?limit=8", headers={
        "User-Agent": "Roxi/0.1 (https://github.com/RejectKid/roxi)",
        "Accept": "application/geo+json",
    })
    with build_opener(NoRedirect()).open(request, timeout=15) as response:
        return read_bounded(response)


def fetch_weather(exercise_id: str, source_key: str) -> Receipt:
    with connect_read() as conn:
        source = registered(conn, exercise_id, source_key)
    station = source["nws_station"]
    if not station:
        raise CatalogError(422, "Register an NWS station on this weather source first.")
    try:
        payload = request_weather(station)
    except HTTPError as error:
        error.close()
        return record_failure(exercise_id, source_key, "nws", f"NWS responded with HTTP {error.code}.")
    except (URLError, OSError):
        return record_failure(exercise_id, source_key, "nws", "Could not reach the NWS observation service.")
    except ValueError as error:
        return record_failure(exercise_id, source_key, "nws", str(error))
    return ingest(exercise_id, source_key, f"nws-{station}.geojson", payload, None, "nws")


def find_capture(conn, exercise_id, source_key, capture_id):
    capture = conn.execute(
        "SELECT * FROM catalog_capture WHERE exercise_id = %s AND source_key = %s AND id = %s",
        (exercise_id, source_key, capture_id),
    ).fetchone()
    if not capture:
        raise CatalogError(404, "Source capture not found.")
    return capture


def replay(exercise_id: str, source_key: str, capture_id: int, version: int) -> Receipt:
    with connect_editor() as conn:
        source = registered(conn, exercise_id, source_key, lock=True)
        capture = find_capture(conn, exercise_id, source_key, capture_id)
        adapter = ADAPTERS[source["adapter_key"]]
        selected = adapter.select_version(version)
        try:
            with archive_path(capture["digest"]).open("rb") as stream:
                payload = read_bounded(stream)
        except (OSError, ValueError):
            raise CatalogError(410, "Original source capture is unavailable.") from None
        if hashlib.sha256(payload).hexdigest() != capture["digest"]:
            raise CatalogError(410, "Original source capture failed its digest check.")
        fields = derive(conn, capture_id, adapter, selected, payload)
    return Receipt(source_key, capture_id, selected, **fields)


def attempt_history(conn, exercise_id, source_key):
    return conn.execute(
        """
        SELECT a.*, latest.state, fetched.attempted_at AS last_successful_fetch, fetched.capture_id AS last_fetched_id
        FROM catalog_attempt AS a
        LEFT JOIN LATERAL (
            SELECT state FROM catalog_derivation WHERE capture_id = a.capture_id ORDER BY parser_version DESC LIMIT 1
        ) AS latest ON true
        LEFT JOIN LATERAL (
            SELECT capture_id, attempted_at FROM catalog_attempt
            WHERE exercise_id = a.exercise_id AND source_key = a.source_key AND capture_id IS NOT NULL
            ORDER BY id DESC LIMIT 1
        ) AS fetched ON true
        WHERE a.exercise_id = %s AND a.source_key = %s ORDER BY a.id DESC LIMIT 1
        """,
        (exercise_id, source_key),
    ).fetchone()


def latest_usable(conn, exercise_id, source_key):
    return conn.execute(
        """
        SELECT c.id, c.captured_at, d.*
        FROM catalog_capture AS c
        JOIN LATERAL (
            SELECT * FROM catalog_derivation WHERE capture_id = c.id ORDER BY parser_version DESC LIMIT 1
        ) AS d ON true
        WHERE c.exercise_id = %s AND c.source_key = %s AND d.state <> 'quarantined'
        ORDER BY d.observed_at DESC, c.id DESC LIMIT 1
        """,
        (exercise_id, source_key),
    ).fetchone()


def capture_url(exercise_id: str, source_key: str, capture_id: int) -> str:
    return f"/exercises/{exercise_id}/sources/{source_key}/captures/{capture_id}"


def recent_captures(conn, exercise_id, source_key):
    rows = conn.execute(
        """
        SELECT c.id, c.captured_at, d.state
        FROM catalog_capture AS c
        JOIN LATERAL (
            SELECT state FROM catalog_derivation WHERE capture_id = c.id ORDER BY parser_version DESC LIMIT 1
        ) AS d ON true
        WHERE c.exercise_id = %s AND c.source_key = %s ORDER BY c.id DESC LIMIT 8
        """,
        (exercise_id, source_key),
    ).fetchall()
    return [{**row, "url": capture_url(exercise_id, source_key, row["id"])} for row in rows]


def pending_audio(conn, exercise_id, source_key) -> int:
    return conn.execute(
        """
        SELECT count(*) AS pending FROM channel_capture AS c
        WHERE c.exercise_id = %s AND c.source_key = %s AND c.audio_digest IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM catalog_derivation AS d WHERE d.capture_id = c.capture_id)
        """,
        (exercise_id, source_key),
    ).fetchone()["pending"]


def describe(conn, config, now):
    exercise, source = config["exercise_id"], config["source_key"]
    attempt = attempt_history(conn, exercise, source)
    usable = latest_usable(conn, exercise, source)
    adapter = ADAPTERS[config["adapter_key"]]
    observed = usable["observed_at"] if usable else None
    subjects = {item["subject_id"] for item in usable["records"]} if usable else set()
    missing = set(config["expected_ids"]) - subjects
    gaps = []
    if missing:
        gaps.append("Missing expected IDs: " + ", ".join(sorted(missing)))
    if not usable:
        gaps.append("No usable capture.")
    pending = pending_audio(conn, exercise, source) if config["adapter_key"] in CHANNEL_ADAPTERS else 0
    if pending:
        gaps.append(f"{pending} completed radio clip(s) awaiting transcription.")
    if attempt and attempt["failure"]:
        gaps.append(attempt["failure"])
    if attempt and attempt["state"] == "quarantined":
        gaps.append("The latest capture was quarantined.")
    elif attempt and attempt["state"] == "partial" and (not usable or attempt["capture_id"] != usable["id"]):
        gaps.append("The latest capture contains gaps. Open it to inspect the details.")
    if usable and usable["issues"]:
        count = len(usable["issues"])
        gaps.append(f"{count} {'issue' if count == 1 else 'issues'} in the current capture.")
    age = (now - observed).total_seconds() if observed else None
    if age is not None and age > config["stale_after_seconds"]:
        gaps.append("No observation within the freshness window.")
    if age is not None and age < -60:
        gaps.append("The observation time is ahead of the local clock.")

    status = "missing"
    if usable:
        outage = attempt and (attempt["failure"] or attempt["state"] == "quarantined")
        stale = outage or age > config["stale_after_seconds"] or age < -60
        status = "stale" if stale else "partial" if gaps else "fresh"
    capture_lag = (usable["captured_at"] - observed).total_seconds() if usable else None
    return {**config, "kind": "adapter", "status": status,
            "contract_version": usable["contract_version"] if usable else CONTRACT_VERSION,
            "input_schema": usable["input_schema"] if usable else adapter.input_schema,
            "parser_version": usable["parser_version"] if usable else None,
            "last_attempt_at": attempt["attempted_at"] if attempt else None,
            "last_successful_fetch": attempt["last_successful_fetch"] if attempt else None,
            "observed_at": observed,
            "latency_seconds": capture_lag if capture_lag is not None and capture_lag >= 0 else None,
            "record_count": len(usable["records"]) if usable else 0, "missing_ids": sorted(missing), "gaps": gaps,
            "capture_url": capture_url(exercise, source, usable["id"]) if usable else None,
            "latest_capture_url": (capture_url(exercise, source, attempt["last_fetched_id"])
                                   if attempt and attempt["last_fetched_id"] else None),
            "recent_captures": recent_captures(conn, exercise, source)}


def recorded_sources(conn, exercise_id):
    rows = conn.execute(
        """
        SELECT source.source_key, source.kind, source.id, source.original_name, source.imported_at,
               (SELECT count(*) FROM source_record WHERE source_file_id = source.id) AS record_count,
               (SELECT count(*) FROM import_issue WHERE source_file_id = source.id) AS issues,
               source.exercise_offset_ms
        FROM exercise_source AS current
        JOIN source_file AS source ON source.id = current.source_file_id
        WHERE current.exercise_id = %s ORDER BY current.position
        """,
        (exercise_id,),
    ).fetchall()
    result = []
    for row in rows:
        gaps = []
        if row["issues"]:
            gaps.append(f"{row['issues']} malformed imported line(s).")
        if row["kind"] == "audio" and row["exercise_offset_ms"] is None:
            gaps.append("Exercise time is unknown.")
        result.append({**row, "label": row["original_name"], "status": "recorded", "gaps": gaps,
                       "input_schema": f"roxi.recorded-{row['kind']}/v1", "last_successful_fetch": row["imported_at"],
                       "capture_url": f"/exercises/{exercise_id}/{row['kind']}/{row['id']}"})
    return result


def sandbox_source(conn, exercise_id, now):
    room = conn.execute("SELECT room_key FROM live_room WHERE exercise_id = %s", (exercise_id,)).fetchone()
    if not room:
        return []
    row = conn.execute(
        """
        SELECT id, received_at, classified_at,
            (SELECT count(*) FROM live_chat_version WHERE exercise_id = %s AND change_seq IS NULL) AS pending
        FROM live_chat_version WHERE exercise_id = %s ORDER BY id DESC LIMIT 1
        """,
        (exercise_id, exercise_id),
    ).fetchone()
    latest = row["received_at"] if row else None
    gaps = []
    if not row:
        gaps.append("Waiting for the first sandbox message.")
    if row and row["pending"]:
        gaps.append(f"{row['pending']} message version(s) waiting for classification.")
    if latest and (now - latest).total_seconds() > 300:
        gaps.append("No sandbox message in the last five minutes.")
    return [{"source_key": room["room_key"], "label": f"#{room['room_key']}", "kind": "sandbox-chat",
             "input_schema": "roxi.sandbox-chat/v1", "parser_version": 1, "contract_version": CONTRACT_VERSION,
             "status": ("missing" if not row else "stale" if (now - latest).total_seconds() > 300
                        else "partial" if gaps else "fresh"),
             "last_successful_fetch": latest, "gaps": gaps,
             "latency_seconds": ((row["classified_at"] - latest).total_seconds() if row and row["classified_at"] else None),
             "capture_url": f"/exercises/{exercise_id}/live/evidence/{row['id']}" if row else None}]


def catalog(exercise_id: str, now: datetime | None = None, *, allowed: frozenset[str] | None = None) -> dict:
    now = now or datetime.now(UTC)
    with connect_read() as conn:
        exercise = conn.execute("SELECT id, name FROM exercise WHERE id = %s", (exercise_id,)).fetchone()
        if not exercise:
            raise CatalogError(404, "Exercise not found.")
        configs = conn.execute(
            "SELECT * FROM catalog_source WHERE exercise_id = %s ORDER BY source_key", (exercise_id,),
        ).fetchall()
        sources = [describe(conn, config, now) for config in configs
                   if allowed is None or f"catalog:{config['source_key']}" in allowed]
        sources.extend(row for row in sandbox_source(conn, exercise_id, now)
                       if allowed is None or f"chat:{row['source_key']}" in allowed)
        sources.extend(row for row in recorded_sources(conn, exercise_id)
                       if allowed is None or f"recorded:{row['source_key']}" in allowed)
        return {"exercise": exercise, "checked_at": now, "sources": sources}


def capture_details(exercise_id: str, source_key: str, capture_id: int) -> dict:
    with connect_read() as conn:
        source = registered(conn, exercise_id, source_key)
        capture = find_capture(conn, exercise_id, source_key, capture_id)
        exercise = conn.execute("SELECT id, name FROM exercise WHERE id = %s", (exercise_id,)).fetchone()
        derivations = conn.execute(
            "SELECT * FROM catalog_derivation WHERE capture_id = %s ORDER BY parser_version DESC", (capture_id,),
        ).fetchall()
        for row in derivations:
            row["record_count"] = len(row["records"])
            row["issue_count"] = len(row["issues"])
            row["records"] = row["records"][:ITEM_LIMIT]
            row["issues"] = row["issues"][:ITEM_LIMIT]
    return {"exercise": exercise, "source": source, "capture": capture, "derivations": derivations}
