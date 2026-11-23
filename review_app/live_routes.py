import asyncio
import os
from secrets import compare_digest
from typing import Annotated

import psycopg
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from psycopg.rows import dict_row
from pydantic import Field, ValidationError

from . import live_chat as live
from . import pilot_access as access
from .importer import Slug
from .review_routes import require_local_json
from .storage import READER_DSN, connect_read
from .views import templates

router = APIRouter(prefix="/exercises/{exercise_id}/live", tags=["Sandbox chat"])
ItemId = Annotated[int, Field(gt=0, le=2**63 - 1)]
POLL_SECONDS = 0.2


@router.post("/{room_key}/messages", status_code=202, responses={200: {"description": "Duplicate delivery"}})
async def receive_message(
    exercise_id: Slug, room_key: Slug, request: Request,
    sandbox_token: Annotated[str, Header(alias="X-Roxi-Sandbox-Token")] = "",
):
    require_local_json(request)
    if access.demo():
        configured = os.environ.get("ROXI_SANDBOX_TOKEN")
        if not configured:
            raise HTTPException(503, "Set ROXI_SANDBOX_TOKEN to enable sandbox messages.")
        if not compare_digest(configured.encode(), sandbox_token.encode()):
            raise HTTPException(401, "The sandbox token is missing or invalid.")
        await asyncio.to_thread(access.require, exercise_id)
    else:
        await asyncio.to_thread(access.require_ingest, exercise_id, f"chat:{room_key}")

    original = bytearray()
    async for chunk in request.stream():
        if len(original) + len(chunk) > live.MAX_PAYLOAD:
            raise HTTPException(413, "The sandbox message is too large.")
        original.extend(chunk)
    try:
        message = live.ChatMessage.model_validate_json(original)
    except ValidationError:
        raise HTTPException(422, "Supply message_id, version, sent_ms, sender, and nonblank single-line text.") from None
    def ingest():
        if not access.demo():
            access.require_ingest(exercise_id, f"chat:{room_key}")
        return live.receive(exercise_id, room_key, message, bytes(original))
    receipt = await asyncio.to_thread(ingest)
    return JSONResponse(receipt, status_code=200 if receipt["duplicate"] else 202)


def read_changes(conn, exercise_id: str, cursor: int) -> list[dict]:
    return conn.execute(
        f"SELECT {live.ITEM_COLUMNS} FROM live_chat_version "
        "WHERE exercise_id = %s AND change_seq > %s ORDER BY change_seq LIMIT %s",
        (exercise_id, cursor, live.BATCH_SIZE),
    ).fetchall()


def room_access(exercise_id: str) -> access.Grant:
    access.require(exercise_id, full=False)
    with connect_read() as conn:
        room_key = live.room_for(conn, exercise_id)["room_key"]
    return access.require(exercise_id, full=False, source=f"chat:{room_key}")


async def changes(exercise_id: str, cursor: int):
    conn = await asyncio.to_thread(psycopg.connect, READER_DSN, connect_timeout=5,
                                   autocommit=True, row_factory=dict_row)
    try:
        quiet = 0
        while True:
            try:
                await asyncio.to_thread(room_access, exercise_id)
            except (access.AccessError, live.LiveError):
                return
            rows = await asyncio.to_thread(read_changes, conn, exercise_id, cursor)
            if rows:
                cursor = rows[-1]["change_seq"]
                quiet = 0
                yield live.encode_feed("updates", cursor, rows)
                continue
            quiet += 1
            if quiet == 75:
                yield ": heartbeat\n\n"
                quiet = 0
            await asyncio.sleep(POLL_SECONDS)
    finally:
        conn.close()


@router.get("/feed")
async def stream(
    exercise_id: Slug, last_event_id: Annotated[str, Header(max_length=19)] = "",
):
    await asyncio.to_thread(room_access, exercise_id)
    seq, items = await asyncio.to_thread(live.snapshot, exercise_id)
    resume = last_event_id.isascii() and last_event_id.isdecimal() and int(last_event_id) <= seq

    async def frames():
        cursor = int(last_event_id) if resume else seq
        if not resume:
            yield live.encode_feed("snapshot", seq, items)
        else:
            yield ": resumed\n\n"
        async for frame in changes(exercise_id, cursor):
            yield frame

    return StreamingResponse(frames(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.get("/evidence/{item_id}", response_class=HTMLResponse)
def evidence_page(request: Request, exercise_id: Slug, item_id: ItemId):
    grant = room_access(exercise_id)
    exercise, record, history = live.original_evidence(exercise_id, item_id)
    return templates.TemplateResponse(request, "live_evidence.html", {
        "exercise": exercise, "record": record, "history": history, "nav_section": "timeline", "grant": grant,
    })


@router.get("/evidence/{item_id}/original")
def original_message(exercise_id: Slug, item_id: ItemId):
    room_access(exercise_id)
    _, record, _ = live.original_evidence(exercise_id, item_id)
    return Response(record["original"], media_type="application/json", headers={"X-Content-Type-Options": "nosniff"})
