from collections import defaultdict
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from .history_registry import exercise_for
from .source_catalog import capture_url
from .storage import connect_read

DISPLAY_LIMIT = 250


class HistoryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: AwareDatetime | None = None
    known_at: AwareDatetime | None = None

    @field_validator("as_of", "known_at", mode="before")
    @classmethod
    def optional_timestamp(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


def catalog_observations(conn, exercise_id, known_at, as_of, allowed):
    return conn.execute(
        """
        WITH known AS (
            SELECT capture.source_key, capture.id, capture.captured_at AS received_at,
                   derived.derived_at AS available_at, derived.parser_version,
                   item.record, item.ordinal,
                   (item.record ->> 'observed_at')::timestamptz AS observed_at
            FROM catalog_capture AS capture
            JOIN LATERAL (
                SELECT * FROM catalog_derivation
                WHERE capture_id = capture.id AND derived_at <= %(known)s
                ORDER BY parser_version DESC LIMIT 1
            ) AS derived ON true
            CROSS JOIN LATERAL jsonb_array_elements(derived.records) WITH ORDINALITY AS item(record, ordinal)
            WHERE capture.exercise_id = %(exercise)s AND capture.captured_at <= %(known)s
              AND (%(allowed)s::text[] IS NULL OR capture.source_key = ANY(%(allowed)s))
        ), arrivals AS (
            SELECT *, max(observed_at) OVER (
                PARTITION BY source_key ORDER BY received_at, id
                GROUPS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS prior_observation
            FROM known
        ), records AS (
            SELECT DISTINCT ON (source_key, (record ->> 'record_id'), observed_at) *
            FROM arrivals
            WHERE %(cutoff)s::timestamptz IS NULL OR observed_at <= %(cutoff)s
            ORDER BY source_key, (record ->> 'record_id'), observed_at, received_at DESC, id DESC
        )
        SELECT * FROM records
        ORDER BY observed_at DESC, received_at DESC, id DESC, ordinal LIMIT %(limit)s
        """,
        {"exercise": exercise_id, "known": known_at, "cutoff": as_of, "limit": DISPLAY_LIMIT + 1,
         "allowed": allowed},
    ).fetchall()


def chat_observations(conn, exercise_id, known_at, as_of, allowed):
    return conn.execute(
        """
        WITH messages AS (
            SELECT DISTINCT ON (version.message_id)
                room.room_key, version.id, version.message_id, version.version, version.sender,
                version.body, version.sent_at_utc AS observed_at, version.received_at,
                version.classified_at AS available_at, version.kind, version.subject_id,
                version.reported_status, version.late
            FROM live_chat_version AS version
            JOIN live_room AS room USING (exercise_id)
            WHERE version.exercise_id = %(exercise)s AND version.received_at <= %(known)s
              AND version.classified_at <= %(known)s
              AND (%(allowed)s::text[] IS NULL OR room.room_key = ANY(%(allowed)s))
            ORDER BY version.message_id, version.version DESC
        ), reports AS (
            SELECT *, count(*) FILTER (WHERE observed_at IS NULL) OVER () AS untimed_count
            FROM messages WHERE kind <> 'chatter'
        )
        SELECT * FROM reports
        ORDER BY
            CASE WHEN %(cutoff)s::timestamptz IS NULL OR observed_at <= %(cutoff)s THEN 0 ELSE 1 END,
            observed_at DESC NULLS LAST, received_at DESC, id DESC
        LIMIT %(limit)s
        """,
        {"exercise": exercise_id, "known": known_at, "cutoff": as_of, "limit": DISPLAY_LIMIT + 1,
         "allowed": allowed},
    ).fetchall()


def layers_for(conn, exercise_id, known_at, as_of, allowed):
    rows = conn.execute(
        """
        SELECT DISTINCT ON (layer_key) layer_key, version, label, effective_at, recorded_at, digest, features
        FROM history_layer
        WHERE exercise_id = %s AND recorded_at <= %s AND effective_at <= %s
          AND (%s::text[] IS NULL OR layer_key = ANY(%s))
        ORDER BY layer_key, version DESC
        """,
        (exercise_id, known_at, as_of or known_at, allowed, allowed),
    ).fetchall()
    return [{**row, "original_url": f"/exercises/{exercise_id}/history/layers/{row['layer_key']}/{row['version']}/original"}
            for row in rows]


def catalog_item(exercise_id, row):
    record = row["record"]
    data = record["data"]
    url = capture_url(exercise_id, row["source_key"], row["id"])
    operating = {"en_route", "in_transit"}
    status = "in_service" if record["kind"] == "vehicle_observation" and data.get("status") in operating else None
    geometry = [data["longitude"], data["latitude"]] if "longitude" in data and "latitude" in data else None
    return {"key": f"capture-{row['id']}-{row['parser_version']}-{row['ordinal']}",
            "source": f"catalog:{row['source_key']}", "source_key": row["source_key"], "kind": record["kind"],
            "external_id": record["subject_id"], "label": data.get("name", record["subject_id"]),
            "observed_at": row["observed_at"], "received_at": row["received_at"], "available_at": row["available_at"],
            "late": row["prior_observation"] is not None and row["observed_at"] < row["prior_observation"],
            "reported_status": status, "data": data, "position": geometry,
            "evidence": {"locator": record["locator"], "detail_url": url, "original_url": url + "/original",
                         "parser_version": row["parser_version"]}}


def chat_item(exercise_id, row):
    url = f"/exercises/{exercise_id}/live/evidence/{row['id']}"
    return {"key": f"chat-{row['id']}", "source": f"chat:{row['room_key']}", "source_key": row["room_key"],
            "kind": row["kind"], "external_id": row["subject_id"], "label": row["body"],
            "observed_at": row["observed_at"], "received_at": row["received_at"], "available_at": row["available_at"],
            "late": row["late"], "reported_status": row["reported_status"], "position": None,
            "data": {"sender": row["sender"], "message_id": row["message_id"], "version": row["version"]},
            "evidence": {"locator": {"kind": "message", "value": row["message_id"], "version": row["version"]},
                         "detail_url": url, "original_url": url + "/original"}}


def attach_identities(items: list[dict], registry: dict) -> None:
    entities = {item["id"]: item for item in registry.get("entities", [])}
    aliases = {(entry["source"], entry["external_id"].casefold()): entry["entities"]
               for entry in registry.get("aliases", [])}
    for item in items:
        external = item["external_id"]
        candidates = [entities[key] for key in aliases.get((item["source"], (external or "").casefold()), ())]
        item["entity"] = candidates[0] if len(candidates) == 1 else None
        item["candidates"] = candidates if len(candidates) > 1 else []
        item["identity"] = "matched" if item["entity"] else "ambiguous" if item["candidates"] else "unresolved"


def attach_conflicts(items: list[dict]) -> None:
    groups = defaultdict(list)
    for item in items:
        item["conflicts"] = []
        if item["entity"] and item["observed_at"] and item["reported_status"] in {"in_service", "out_of_service"}:
            groups[item["entity"]["id"], item["observed_at"]].append(item)
    for group in groups.values():
        for item in group:
            item["conflicts"] = [peer["key"] for peer in group if peer["reported_status"] != item["reported_status"]]


def source_keys(sources: frozenset[str] | None, namespace: str) -> list[str] | None:
    return None if sources is None else [source.split(":", 1)[1] for source in sources if source.startswith(namespace + ":")]


def view(exercise_id: str, filters: HistoryFilters, sources: frozenset[str] | None = None) -> dict:
    known_at = filters.known_at or datetime.now(UTC)
    as_of = filters.as_of
    with connect_read() as conn:
        exercise = exercise_for(conn, exercise_id)
        registry_row = conn.execute(
            "SELECT version, reviewed_at, digest, registry FROM history_registry "
            "WHERE exercise_id = %s AND reviewed_at <= %s ORDER BY version DESC LIMIT 1",
            (exercise_id, known_at),
        ).fetchone() if sources is None or "history:registry" in sources else None
        layers = layers_for(conn, exercise_id, known_at, as_of, source_keys(sources, "map"))
        catalog = catalog_observations(conn, exercise_id, known_at, as_of, source_keys(sources, "catalog"))
        chat = chat_observations(conn, exercise_id, known_at, as_of, source_keys(sources, "chat"))

    registry = registry_row["registry"] if registry_row else {}
    items = [catalog_item(exercise_id, row) for row in catalog]
    items.extend(chat_item(exercise_id, row) for row in chat
                 if as_of is None or row["observed_at"] and row["observed_at"] <= as_of)
    items.sort(key=lambda row: (row["observed_at"] or row["received_at"], row["available_at"], row["key"]), reverse=True)
    truncated = len(items) > DISPLAY_LIMIT
    items = items[:DISPLAY_LIMIT]
    attach_identities(items, registry)
    attach_conflicts(items)
    return {"exercise": exercise, "as_of": as_of, "known_at": known_at, "truncated": truncated,
            "untimed_excluded": chat[0]["untimed_count"] if chat and as_of is not None else 0,
            "registry": {"version": registry_row["version"], "reviewed_at": registry_row["reviewed_at"],
                         "reviewer": registry["reviewer"], "digest": registry_row["digest"],
                         "original_url": f"/exercises/{exercise_id}/history/registry/{registry_row['version']}/original"}
            if registry_row else None, "layers": layers, "observations": items}
