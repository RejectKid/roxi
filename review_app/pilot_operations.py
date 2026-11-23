from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from . import live_chat as live
from . import pilot_access as access
from .importer import Slug
from .source_catalog import catalog
from .storage import connect_read
from .views import templates

router = APIRouter(tags=["Pilot operations"])
CONTACT_SECONDS = 300
BACKLOG_SECONDS = 30


def heartbeat(exercise_id: str, room_key: str) -> dict:
    access.require_ingest(exercise_id, f"chat:{room_key}")
    with live.connect_editor() as conn:
        room = live.room_for(conn, exercise_id, lock=True)
        if room["room_key"] != room_key:
            raise access.AccessError(404, "Exercise or source not found.")
        row = conn.execute(
            "INSERT INTO pilot_chat_heartbeat VALUES (%s, clock_timestamp()) ON CONFLICT (exercise_id) "
            "DO UPDATE SET seen_at = clock_timestamp() RETURNING seen_at", (exercise_id,),
        ).fetchone()
    return {"seen_at": row["seen_at"]}


@router.post("/exercises/{exercise_id}/live/{room_key}/heartbeat")
def chat_heartbeat(exercise_id: Slug, room_key: Slug):
    return heartbeat(exercise_id, room_key)


def connector_health(exercise_id: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    data = catalog(exercise_id, now)
    connectors = [
        {key: row.get(key) for key in ("source_key", "label", "status", "last_attempt_at",
                                        "last_successful_fetch", "latency_seconds", "gaps")}
        for row in data["sources"] if row["kind"] == "adapter"
    ]
    with connect_read() as conn:
        state = conn.execute(
            """
            SELECT room.room_key, room.change_seq, h.seen_at,
                   aggregate.last_received, aggregate.pending, aggregate.oldest_pending,
                   aggregate.last_classified
            FROM live_room AS room LEFT JOIN pilot_chat_heartbeat AS h USING (exercise_id)
            CROSS JOIN LATERAL (
                SELECT max(received_at) AS last_received, max(classified_at) AS last_classified,
                       count(*) FILTER (WHERE classified_at IS NULL) AS pending,
                       min(received_at) FILTER (WHERE classified_at IS NULL) AS oldest_pending
                FROM live_chat_version AS m WHERE m.exercise_id = room.exercise_id
            ) AS aggregate
            WHERE room.exercise_id = %s
            """,
            (exercise_id,),
        ).fetchone()
    if state:
        stamps = [stamp for stamp in (state["seen_at"], state["last_received"]) if stamp]
        last_contact = max(stamps, default=None)
        age = (now - last_contact).total_seconds() if last_contact else None
        backlog = (now - state["oldest_pending"]).total_seconds() if state["oldest_pending"] else None
        state["connection"] = "waiting" if age is None else "disconnected" if age > CONTACT_SECONDS else "connected"
        state["classifier"] = "backlogged" if backlog is not None and backlog > BACKLOG_SECONDS else (
            "pending" if state["pending"] else "clear"
        )
        state["oldest_pending_seconds"] = round(backlog, 2) if backlog is not None else None
    return {"exercise": data["exercise"], "checked_at": now, "connectors": connectors, "chat": state}


@router.get("/exercises/{exercise_id}/operations.json")
def operations_json(exercise_id: Slug):
    access.require(exercise_id, role="admin")
    return connector_health(exercise_id)


@router.get("/exercises/{exercise_id}/operations", response_class=HTMLResponse)
def operations_page(request: Request, exercise_id: Slug):
    grant = access.require(exercise_id, role="admin")
    return templates.TemplateResponse(request, "pilot_operations.html", {
        **connector_health(exercise_id), "grant": grant, "nav_section": "operations",
    })
