import math

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .storage import ROOT, archive_path, connect_read

app = FastAPI(title="Exercise review")
app.mount("/static", StaticFiles(directory=ROOT / "review_app" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "review_app" / "templates")


def clock(milliseconds: int, precise: bool = False) -> str:
    minutes, remainder = divmod(milliseconds, 60000)
    seconds, fractional = divmod(remainder, 1000)
    result = f"{minutes:02d}:{seconds:02d}"
    return f"{result}.{fractional:03d}" if precise else result


templates.env.filters["clock"] = clock


@app.exception_handler(psycopg.OperationalError)
async def database_unavailable(_request: Request, _error: psycopg.OperationalError):
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
            exercise = conn.execute("SELECT id, name FROM exercise ORDER BY imported_at DESC, id LIMIT 1").fetchone()
            if exercise is None:
                return templates.TemplateResponse(request, "empty.html", {})
        else:
            exercise = conn.execute("SELECT id, name FROM exercise WHERE id = %s", (exercise_id,)).fetchone()
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
            "aligned": aligned,
            "unaligned": unaligned,
            "duration": duration,
            "ticks": ticks,
            "initial_clip": initial_clip,
            "issues": issues,
        },
    )


@app.get("/exercises/{exercise_id}/audio/{source_id}")
def recording(exercise_id: str, source_id: int):
    with connect_read() as conn:
        source = conn.execute(
            "SELECT content_sha256 FROM source_file WHERE id = %s AND exercise_id = %s AND kind = 'audio'",
            (source_id, exercise_id),
        ).fetchone()
    if source is None:
        raise HTTPException(404, "Recording not found.")
    path = archive_path(source["content_sha256"])
    if not path.is_file():
        raise HTTPException(410, "The recording is unavailable in the local archive.")
    return FileResponse(path, media_type="audio/wav", headers={"X-Content-Type-Options": "nosniff"})
