import math

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import query_service
from .assistant_routes import router as assistant_router
from .extraction import subjects
from .history_registry import HistoryError
from .history_routes import router as history_router
from .live_chat import LiveError, registered_room
from .live_routes import router as live_router
from .query_routes import router as query_router
from .review_routes import router as review_router
from .review_store import ReviewError
from .source_catalog import CatalogError
from .source_routes import router as source_router
from .storage import ROOT, archive_path, connect_read
from .views import templates

app = FastAPI(title="Exercise review")
app.mount("/static", StaticFiles(directory=ROOT / "review_app" / "static"), name="static")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.include_router(review_router)
app.include_router(query_router)
app.include_router(assistant_router)
app.include_router(live_router)
app.include_router(source_router)
app.include_router(history_router)


@app.exception_handler(ReviewError)
@app.exception_handler(LiveError)
@app.exception_handler(CatalogError)
@app.exception_handler(HistoryError)
async def review_error(_request: Request, error: ReviewError | LiveError | CatalogError | HistoryError):
    return JSONResponse({"detail": error.detail}, status_code=error.status)


@app.exception_handler(query_service.QueryError)
async def query_error(_request: Request, error: query_service.QueryError):
    return JSONResponse(error.result().model_dump(mode="json"), status_code=error.status)


@app.exception_handler(RequestValidationError)
async def validate_request(request: Request, error: RequestValidationError):
    if request.url.path.startswith("/v1/"):
        return await query_error(request, query_service.invalid())
    return await request_validation_exception_handler(request, error)


@app.exception_handler(psycopg.OperationalError)
async def database_unavailable(request: Request, _error: psycopg.OperationalError):
    if request.url.path.startswith("/v1/"):
        return await query_error(request, query_service.unavailable())
    return JSONResponse({"detail": "The local PostgreSQL database is unavailable."}, status_code=503)


@app.get("/healthz")
def health():
    with connect_read() as conn:
        conn.execute("SELECT id FROM exercise LIMIT 1")
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
@app.get("/exercises/{exercise_id}", response_class=HTMLResponse)
def timeline(request: Request, exercise_id: str | None = None):
    with connect_read() as conn:
        if exercise_id is None:
            exercise = conn.execute("SELECT id, name, glossary FROM exercise ORDER BY imported_at DESC, id LIMIT 1").fetchone()
            if exercise is None:
                return templates.TemplateResponse(request, "empty.html", {})
        else:
            exercise = conn.execute("SELECT id, name, glossary FROM exercise WHERE id = %s", (exercise_id,)).fetchone()
            if exercise is None:
                raise HTTPException(404, "Exercise not found.")

        sources = conn.execute(
            """
            SELECT s.id, s.source_key, s.kind, s.original_name, s.exercise_offset_ms, s.duration_ms
            FROM exercise_source AS x JOIN source_file AS s ON s.id = x.source_file_id
            WHERE x.exercise_id = %s ORDER BY x.position
            """,
            (exercise["id"],),
        ).fetchall()
        records = conn.execute(
            """
            SELECT r.*, s.source_key, s.kind, s.original_name
            FROM exercise_source AS x
            JOIN source_file AS s ON s.id = x.source_file_id
            JOIN source_record AS r ON r.source_file_id = s.id
            WHERE x.exercise_id = %s
            ORDER BY r.capture_offset_ms NULLS LAST, x.position, r.ordinal
            """,
            (exercise["id"],),
        ).fetchall()
        issues = conn.execute(
            """
            SELECT i.line_number, i.reason, i.original_text, s.original_name, s.kind
            FROM exercise_source AS x
            JOIN source_file AS s ON s.id = x.source_file_id
            JOIN import_issue AS i ON i.source_file_id = s.id
            WHERE x.exercise_id = %s ORDER BY x.position, i.line_number
            """,
            (exercise["id"],),
        ).fetchall()
        room = registered_room(conn, exercise["id"])

    for record in records:
        record["key"] = f"record-{record['source_file_id']}-{record['ordinal']}"
        record["speaker"] = record["speaker"] or "Unknown speaker"
    aligned = [record for record in records if record["capture_offset_ms"] is not None]
    unaligned = [source for source in sources if source["kind"] == "audio" and source["exercise_offset_ms"] is None]
    for source in unaligned:
        label = source["source_key"].replace("-", " ").capitalize()
        source["label"] = label if label.endswith("radio") else label + " radio"
        source["clips"] = [record for record in records if record["source_file_id"] == source["id"]]

    audio_ends = [source["exercise_offset_ms"] + source["duration_ms"] for source in sources
                  if source["kind"] == "audio" and source["exercise_offset_ms"] is not None]
    last_capture = max((record["capture_offset_ms"] for record in aligned), default=0)
    duration = max(1000, last_capture, max(audio_ends, default=0))
    ticks = sorted({math.floor(duration * step / 5 / 1000) * 1000 for step in range(5)} | {duration})
    initial_clip = next((record for record in aligned if record["kind"] == "audio"), None)
    if initial_clip is None:
        initial_clip = next((record for record in records if record["kind"] == "audio"), None)
    return templates.TemplateResponse(
        request,
        "timeline.html",
        {
            "exercise": exercise,
            "nav_section": "timeline",
            "aligned": aligned,
            "unaligned": unaligned,
            "duration": duration,
            "ticks": ticks,
            "initial_clip": initial_clip,
            "issues": issues,
            "live_room": room,
            "live_subjects": subjects(exercise["glossary"]) if room else {},
        },
    )


def original_file(exercise_id: str, source_id: int, kind: str, media_type: str):
    with connect_read() as conn:
        source = conn.execute(
            "SELECT content_sha256 FROM source_file WHERE id = %s AND exercise_id = %s AND kind = %s",
            (source_id, exercise_id, kind),
        ).fetchone()
    if source is None:
        raise HTTPException(404, "Source file not found.")
    path = archive_path(source["content_sha256"])
    if not path.is_file():
        raise HTTPException(410, "The source file is unavailable in the local archive.")
    return FileResponse(path, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})


@app.get("/exercises/{exercise_id}/audio/{source_id}")
def recording(exercise_id: str, source_id: int):
    return original_file(exercise_id, source_id, "audio", "audio/wav")


@app.get("/exercises/{exercise_id}/chat/{source_id}")
def chat_log(exercise_id: str, source_id: int):
    return original_file(exercise_id, source_id, "chat", "text/plain")
