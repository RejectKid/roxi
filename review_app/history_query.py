import csv
import hashlib
import io
import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime

from . import history_models as model
from . import pilot_access as access
from . import query_service as query
from .channel_capture import ChannelMessage, CompletedClip, wave_input
from .history_registry import HistoryError, Registry
from .history_view import HistoryFilters, source_keys, view
from .source_catalog import CatalogError, read_bounded
from .storage import archive_path, connect_read

STATUS = {"in_service": "in service", "out_of_service": "out of service", "unknown": "status unknown"}


def pinned_view(request: model.HistoryPin, sources: frozenset[str] | None) -> dict:
    if request.known_at > datetime.now(UTC):
        raise query.invalid("The known-by cutoff must be in the past or present.")
    try:
        snapshot = view(request.exercise_id, HistoryFilters(as_of=request.as_of, known_at=request.known_at), sources)
    except HistoryError:
        raise query.not_found("Exercise history not found.") from None
    for row in snapshot["observations"]:
        row["registry"] = snapshot["registry"]
    return snapshot


def observation_identity(row: dict) -> tuple:
    return (row["source"], row["record_id"], None if row["data"].get("medium") == "chat" else row["observed_at"])


def subject(row: dict) -> str:
    entity = row["entity"]
    return entity["label"] if entity else row["external_id"] or "Unresolved subject"


def summary(row: dict) -> str:
    name = subject(row)
    if row["kind"] == "vehicle_observation":
        text = f"{row['source_key']} observed {name} with telemetry status {row['data']['status']}."
    elif row["reported_status"] in STATUS:
        text = f"{row['source_key']} reported {name} {STATUS[row['reported_status']]}."
    else:
        text = f"{row['source_key']} recorded a {row['kind'].replace('_', ' ')} for {name}."
    if row["late"]:
        text += " The record arrived out of observation order."
    if row["candidates"]:
        text += " Reviewed aliases leave its identity unresolved."
    return text


def config_at(conn, request: model.HistoryPin, sources: frozenset[str] | None) -> list[dict]:
    return conn.execute(
        """
        SELECT source.source_key, source.label, source.stale_after_seconds, source.expected_ids,
               attempt.failure, derived.state, pending.count AS pending_audio
        FROM catalog_source AS source
        LEFT JOIN LATERAL (
            SELECT capture_id, failure FROM catalog_attempt
            WHERE exercise_id = source.exercise_id AND source_key = source.source_key AND attempted_at <= %(known)s
            ORDER BY attempted_at DESC, id DESC LIMIT 1
        ) AS attempt ON true
        LEFT JOIN LATERAL (
            SELECT state FROM catalog_derivation WHERE capture_id = attempt.capture_id AND derived_at <= %(known)s
            ORDER BY parser_version DESC LIMIT 1
        ) AS derived ON true
        LEFT JOIN LATERAL (
            SELECT count(*) FROM channel_capture item
            JOIN catalog_capture capture ON capture.id = item.capture_id
            WHERE item.exercise_id = source.exercise_id AND item.source_key = source.source_key
              AND item.audio_digest IS NOT NULL AND capture.captured_at <= %(known)s
              AND (%(cutoff)s::timestamptz IS NULL OR item.observed_at <= %(cutoff)s)
              AND NOT EXISTS (SELECT 1 FROM catalog_derivation d
                              WHERE d.capture_id = item.capture_id AND d.derived_at <= %(known)s)
        ) AS pending ON source.adapter_key = 'completed-radio'
        WHERE source.exercise_id = %(exercise)s AND source.registered_at <= %(known)s
          AND (%(allowed)s::text[] IS NULL OR source.source_key = ANY(%(allowed)s))
        ORDER BY source.source_key
        """,
        {"exercise": request.exercise_id, "known": request.known_at, "cutoff": request.as_of,
         "allowed": source_keys(sources, "catalog")},
    ).fetchall()


def source_groups(snapshot: dict, configs: list[dict]) -> tuple[dict, list[str]]:
    groups = defaultdict(list)
    for row in snapshot["observations"]:
        if row["source"].startswith("catalog:"):
            groups[row["source_key"]].append(row)
    gaps = []
    for config in configs:
        rows = groups[config["source_key"]]
        if not rows:
            gaps.append(f"{config['label']} has no usable observation in this view.")
        else:
            missing = sorted(set(config["expected_ids"]) - {row["external_id"] for row in rows})
            if missing:
                gaps.append(f"{config['label']} is missing expected IDs {', '.join(missing)}.")
        if config["pending_audio"]:
            gaps.append(f"{config['label']} has {config['pending_audio']} clip(s) awaiting transcription at this cutoff.")
        if config["failure"]:
            gaps.append(f"{config['label']} had an unavailable fetch at this cutoff.")
        elif config["state"] in {"partial", "quarantined"}:
            gaps.append(f"{config['label']} had a {config['state']} capture at this cutoff.")
    if snapshot["untimed_excluded"]:
        gaps.append(f"{snapshot['untimed_excluded']} observations lacked an event time and were excluded.")
    if snapshot["truncated"]:
        gaps.append("This view contains the newest 250 observations. Older history may be omitted.")
    return groups, gaps


def matches(row: dict, entity_id: str | None) -> bool:
    return entity_id is None or bool(row["entity"] and row["entity"]["id"] == entity_id)


def matching_conflicts(rows: list[dict], entity_id: str | None):
    lookup = {row["key"]: row for row in rows}
    seen = set()
    for row in rows:
        if not matches(row, entity_id):
            continue
        for key in row["conflicts"]:
            pair = tuple(sorted((row["key"], key)))
            if pair not in seen:
                seen.add(pair)
                yield row, lookup[key]


class Originals:
    def __init__(self, conn):
        self.conn = conn
        self.captures: dict[int, tuple[bytes, str]] = {}
        self.registries: dict[str, dict] = {}

    def identity(self, row: dict) -> model.ReviewedIdentity | None:
        if not row["entity"]:
            return None
        registry = row["registry"]
        digest = registry["digest"]
        key = (row["source"], row["external_id"].casefold())
        if digest not in self.registries:
            try:
                original = Registry.model_validate_json(verified_payload(digest))
            except ValueError:
                raise query.QueryError(410, "source_unavailable", "The original identity registry is unavailable.") from None
            entities = {entity.id: entity for entity in original.entities}
            self.registries[digest] = {
                (alias.source, alias.external_id.casefold()): entities[alias.entities[0]]
                for alias in original.aliases if len(alias.entities) == 1
            }
        entity = self.registries[digest].get(key)
        if not entity or entity.model_dump() != row["entity"]:
            raise query.QueryError(410, "source_unavailable", "The original registry does not confirm the reviewed identity.")
        return model.ReviewedIdentity(entity_id=entity.id, label=entity.label, external_id=row["external_id"],
                                      registry_version=registry["version"], registry_url=registry["original_url"],
                                      registry_sha256=digest)

    def read(self, request: model.HistoryPin, row: dict) -> model.OpenHistoryEvidence:
        locator = row["evidence"]["locator"]
        envelope_sha256 = None
        if row["source"].startswith("chat:"):
            source = self.conn.execute(
                "SELECT original FROM live_chat_version WHERE id = %s AND exercise_id = %s",
                (int(row["key"].split("-")[1]), request.exercise_id),
            ).fetchone()
            if not source:
                raise query.not_found("The cited chat message is unavailable.")
            payload = source["original"]
            digest = hashlib.sha256(payload).hexdigest()
        else:
            capture_id = int(row["key"].split("-")[1])
            payload, digest = self.capture(request.exercise_id, row["source_key"], capture_id)
        try:
            if locator["kind"] == "audio_interval":
                envelope_sha256 = digest
                excerpt, digest = self.radio_record(request, row, capture_id, payload)
            else:
                is_channel_chat = row["source"].startswith("catalog:") and row["data"].get("medium") == "chat"
                excerpt = original_record(payload, locator, channel_chat=is_channel_chat)
                if not record_matches(row, excerpt):
                    raise ValueError("Original record does not match its locator.")
                if is_channel_chat:
                    message = ChannelMessage.model_validate(excerpt)
                    if (message.sender, message.observed_at) != (row["data"]["sender"], row["observed_at"]):
                        raise ValueError("Original message metadata does not match.")
        except (ValueError, LookupError, TypeError, csv.Error, RecursionError):
            raise query.QueryError(410, "source_unavailable", "The archived source no longer resolves to this record.") from None
        return model.OpenHistoryEvidence(exercise_id=request.exercise_id, as_of=request.as_of, known_at=request.known_at,
                                         key=row["key"], source=row["source"], sha256=digest,
                                         original_record=excerpt, identity=self.identity(row),
                                         envelope_sha256=envelope_sha256, **row["evidence"])

    def radio_record(self, request, row, capture_id, payload):
        clip = CompletedClip.model_validate_json(payload)
        locator = row["evidence"]["locator"]
        source = self.conn.execute(
            "SELECT audio_digest, duration_ms FROM channel_capture "
            "WHERE exercise_id = %s AND source_key = %s AND capture_id = %s",
            (request.exercise_id, row["source_key"], capture_id),
        ).fetchone()
        if not source or not source["audio_digest"]:
            raise ValueError("The original clip reference is absent.")
        raw = verified_payload(source["audio_digest"])
        try:
            original, duration = wave_input(clip.wav_base64)
        except CatalogError as error:
            raise ValueError("The original clip envelope is invalid.") from error
        if (original != raw or duration != source["duration_ms"] or clip.clip_id != row["data"]["clip_id"]
                or clip.sender != row["data"]["sender"] or clip.observed_at.isoformat() != row["data"]["clip_started_at"]
                or not 0 <= locator["start_ms"] < locator["end_ms"] <= duration
                or int(round((row["observed_at"] - clip.observed_at).total_seconds() * 1000)) != locator["start_ms"]):
            raise ValueError("The audio interval did not match its original clip.")
        return {"clip_id": clip.clip_id, "sender": clip.sender, "clip_started_at": clip.observed_at.isoformat(),
                "generated_transcript": row["label"], "duration_ms": duration}, source["audio_digest"]

    def capture(self, exercise: str, source_key: str, capture_id: int) -> tuple[bytes, str]:
        if capture_id not in self.captures:
            row = self.conn.execute(
                "SELECT digest FROM catalog_capture WHERE id = %s AND exercise_id = %s AND source_key = %s",
                (capture_id, exercise, source_key),
            ).fetchone()
            if not row:
                raise query.not_found("The cited capture is unavailable.")
            self.captures[capture_id] = (verified_payload(row["digest"]), row["digest"])
        return self.captures[capture_id]


def verified_payload(digest: str) -> bytes:
    try:
        with archive_path(digest).open("rb") as stream:
            payload = read_bounded(stream)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("The archived digest differs.")
    except (OSError, ValueError):
        raise query.QueryError(410, "source_unavailable", "The original archive failed verification.") from None
    return payload


def finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("A non-finite value is not valid JSON.")
    return number


def original_record(payload: bytes, locator: dict, *, channel_chat: bool = False) -> dict:
    if locator["kind"] == "csv_row":
        reader = csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
        header = next(reader, [])
        while reader.line_num < locator["value"]:
            physical_line = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                break
            if physical_line == locator["value"]:
                return dict(zip(header, row, strict=True))
        raise ValueError("CSV record missing.")
    document = json.loads(payload, parse_constant=finite_number, parse_float=finite_number)
    if locator["kind"] == "json_pointer" and not (channel_chat and locator["value"] == ""):
        pointer = re.fullmatch(r"/(vehicles|features)/([0-9]+)", locator["value"])
        if pointer is None:
            raise ValueError("Unknown observation pointer.")
        document = document[pointer[1]][int(pointer[2])]
    if not isinstance(document, dict):
        raise ValueError("Original observation is not an object.")
    return document


def record_matches(row: dict, excerpt: dict) -> bool:
    match row["kind"]:
        case "asset":
            return excerpt.get("asset_id") == row["external_id"]
        case "site":
            return excerpt.get("site_id") == row["external_id"]
        case "vehicle_observation":
            return excerpt.get("vehicle_id") == row["external_id"]
        case "weather_observation":
            properties = excerpt.get("properties", {})
            station = properties.get("station") if isinstance(properties, dict) else None
            return isinstance(station, str) and station.rstrip("/").rsplit("/", 1)[-1] == row["external_id"]
        case _:
            message = row["data"]
            return (excerpt.get("message_id"), excerpt.get("version"), excerpt.get("text")) == (
                message["message_id"], message["version"], row["label"],
            )


def open_evidence(request: model.EvidenceRequest) -> model.OpenHistoryEvidence:
    grant = access.require(request.exercise_id, full=False)
    snapshot = pinned_view(request, grant.sources)
    row = next((row for row in snapshot["observations"] if row["key"] == request.key), None)
    if row is None:
        raise query.not_found("Evidence is not part of this pinned history view.")
    with connect_read() as conn:
        return Originals(conn).read(request, row)


def pin_from(request: model.HistoryQuery) -> model.HistoryPin:
    return model.HistoryPin(exercise_id=request.exercise_id, as_of=request.as_of, known_at=request.known_at)


def history_record(row: dict) -> model.HistoryRecord:
    data = row["data"]
    medium = data.get("medium")
    entity = row["entity"]
    return model.HistoryRecord(
        key=row["key"], source=row["source"], record_id=row["record_id"], kind=row["kind"],
        state="proposed" if medium else "observation", observed_at=row["observed_at"],
        received_at=row["received_at"], available_at=row["available_at"], late=row["late"], text=data.get("text"),
        medium=medium, sender=data.get("sender"), input_version=data.get("version"),
        reported_status=row["reported_status"] if medium else None,
        reported_time_seconds=data.get("reported_time_seconds"), telemetry_status=data.get("status"),
        position=row["position"], external_id=row["external_id"], entity_id=entity["id"] if entity else None,
        identity_status=row["identity"], candidate_entity_ids=[item["id"] for item in row["candidates"]],
        candidate_target_keys=row["candidate_targets"],
    )


def candidates(request: model.HistoryQuery, pin: model.HistoryPin, rows: list[dict], groups: dict,
               configs: list[dict], gaps: list[str], sources: frozenset[str] | None):
    if request.mode == "conflicts":
        for row, peer in matching_conflicts(rows, request.entity_id):
            yield "conflict", row, f"{summary(row)} {summary(peer)}", [(pin, row), (pin, peer)]
    elif request.mode == "overdue":
        cutoff = request.as_of or request.known_at
        for config in configs:
            last = next((item for item in groups[config["source_key"]]
                         if item["observed_at"] <= cutoff and matches(item, request.entity_id)), None)
            if last and (cutoff - last["observed_at"]).total_seconds() > config["stale_after_seconds"]:
                yield "overdue", last, f"{config['label']} exceeded its update interval by this cutoff.", [(pin, last)]
    elif request.mode == "changes":
        if not request.baseline_known_at:
            gaps.append("Provide an earlier known-by cutoff to compare changes.")
            return
        baseline = model.HistoryPin(exercise_id=pin.exercise_id, as_of=pin.as_of, known_at=request.baseline_known_at)
        previous = pinned_view(baseline, sources)
        old = {observation_identity(row): row for row in previous["observations"]}
        for row in rows:
            prior = old.pop(observation_identity(row), None)
            if not matches(row, request.entity_id) and (not prior or not matches(prior, request.entity_id)):
                continue
            if prior is None:
                yield "added", row, summary(row), [(pin, row)]
            elif (prior["key"], prior["entity"], prior["candidates"]) != (row["key"], row["entity"], row["candidates"]):
                yield "updated", row, summary(row), [(baseline, prior), (pin, row)]
        if not previous["truncated"] and not any("Older history" in gap for gap in gaps):
            for row in old.values():
                if matches(row, request.entity_id):
                    yield "removed", row, f"The newer view omits the earlier {row['source_key']} record for {subject(row)}.", [(baseline, row)]
    else:
        for row in rows:
            if matches(row, request.entity_id):
                yield "observed", row, summary(row), [(pin, row)]


def query_history(request: model.HistoryQuery) -> model.HistoryResult:
    grant = access.require(request.exercise_id, full=False)
    pin = pin_from(request)
    snapshot = pinned_view(pin, grant.sources)
    fingerprint = query.filter_fingerprint("history", request)
    if grant.sources is not None:
        fingerprint = hashlib.sha256(json.dumps([fingerprint, sorted(grant.sources)]).encode()).hexdigest()
    token = query.decode_cursor(request.cursor, fingerprint)
    offset = token[1] if token else 0
    if token and (token[0] or token[2:] != (0, 0)):
        raise query.invalid("Invalid history cursor.")
    with connect_read() as conn:
        configs = config_at(conn, request, grant.sources)
        groups, gaps = source_groups(snapshot, configs)
        items = list(candidates(request, pin, snapshot["observations"], groups, configs, gaps, grant.sources))
        if not items:
            gaps.append("No matching evidence-backed findings for these cutoffs and filters.")
        sources = Originals(conn)
        facts = []
        for kind, row, text, refs in items[offset:offset + request.limit]:
            try:
                citations = [model.HistoryCitation.model_validate(sources.read(selected, record).model_dump(
                    exclude={"original_record"})) for selected, record in refs]
            except query.QueryError as error:
                gaps.append(error.message)
                continue
            facts.append(model.HistoryFact(kind=kind, subject=subject(row), observed_at=row["observed_at"],
                                           text=text, citations=citations,
                                           records=[history_record(record) for _, record in refs]))
    next_offset = offset + request.limit
    cursor = query.encode_cursor(fingerprint, (False, next_offset, 0, 0)) if next_offset < len(items) else None
    return model.HistoryResult(**pin.model_dump(), mode=request.mode, baseline_known_at=request.baseline_known_at,
                               facts=facts, gaps=list(dict.fromkeys(gaps)), next_cursor=cursor)


def interpret(question: str) -> model.Mode | None:
    if re.search(r"\b(?:overdue|stale|missed updates?)\b", question, re.I):
        return "overdue"
    if re.search(r"\b(?:disagree|conflicts?|contradictions?)\b", question, re.I):
        return "conflicts"
    if re.search(r"\b(?:changed|change|changes|differences?|compare)\b", question, re.I):
        return "changes"
    if re.search(r"\b(?:recorded|record|overview|observations?|reported|known|know)\b", question, re.I):
        return "snapshot"
    return None


def answer_history(request: model.HistoryAnswerRequest) -> model.HistoryAnswer:
    question = model.QuestionRequest.model_validate(request.model_dump(exclude={"exercise_id"}))
    return answer(request.exercise_id, question)


def answer(exercise_id: str, request: model.QuestionRequest) -> model.HistoryAnswer:
    mode = interpret(request.question)
    if mode is None:
        grant = access.require(exercise_id, full=False)
        pin = model.HistoryPin(exercise_id=exercise_id, as_of=request.as_of, known_at=request.known_at)
        pinned_view(pin, grant.sources)
        return model.HistoryAnswer(
            answer="Ask what was recorded, what changed, which reports disagree, or which sources are overdue.",
            result=model.HistoryResult(**pin.model_dump(), mode="snapshot", facts=[],
                                       gaps=["The question did not match a supported history query."]),
        )
    try:
        query_request = model.HistoryQuery(exercise_id=exercise_id, mode=mode, **request.model_dump(exclude={"question"}))
    except ValueError as error:
        raise query.invalid("The baseline must precede the known-by cutoff.") from error
    result = query_history(query_request)
    return model.HistoryAnswer(answer=" ".join(fact.text for fact in result.facts[:3])
                               or "The pinned history has insufficient matching evidence.", result=result)
