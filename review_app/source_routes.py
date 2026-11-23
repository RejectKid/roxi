import asyncio
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import Field

from . import source_catalog as store
from . import pilot_access as access
from .importer import Slug
from .storage import archive_path
from .views import templates

router = APIRouter(tags=["Source catalog"])
CaptureId = Annotated[int, Field(gt=0, le=2**63 - 1)]


@router.get("/exercises/{exercise_id}/sources/catalog.json")
def source_catalog(exercise_id: Slug):
    grant = access.require(exercise_id, full=False)
    return store.catalog(exercise_id, allowed=grant.sources)


@router.get("/exercises/{exercise_id}/sources", response_class=HTMLResponse)
def sources_page(request: Request, exercise_id: Slug):
    grant = access.require(exercise_id, full=False)
    return templates.TemplateResponse(request, "sources.html", {
        **store.catalog(exercise_id, allowed=grant.sources), "nav_section": "sources", "grant": grant,
    })


@router.get("/exercises/{exercise_id}/sources/{source_key}/captures/{capture_id}", response_class=HTMLResponse)
def capture_page(request: Request, exercise_id: Slug, source_key: Slug, capture_id: CaptureId):
    grant = access.require(exercise_id, full=False, source=f"catalog:{source_key}")
    return templates.TemplateResponse(request, "source_capture.html", {
        **store.capture_details(exercise_id, source_key, capture_id), "nav_section": "sources", "grant": grant,
    })


@router.get("/exercises/{exercise_id}/sources/{source_key}/captures/{capture_id}/original", response_class=FileResponse)
def original_capture(exercise_id: Slug, source_key: Slug, capture_id: CaptureId):
    access.require(exercise_id, full=False, source=f"catalog:{source_key}")
    with store.connect_read() as conn:
        capture = store.find_capture(conn, exercise_id, source_key, capture_id)
    original = archive_path(capture["digest"])
    if not original.is_file():
        raise store.CatalogError(410, "Original source capture is unavailable.")
    return FileResponse(original, media_type="application/octet-stream", filename=capture["original_name"],
                        headers={"X-Content-Type-Options": "nosniff"})


@router.post("/exercises/{exercise_id}/sources/{source_key}/captures", status_code=202)
async def receive_capture(request: Request, exercise_id: Slug, source_key: Slug):
    await asyncio.to_thread(access.require_ingest, exercise_id, f"catalog:{source_key}")
    media = request.headers.get("content-type", "").partition(";")[0].strip()
    names = {"text/csv": "capture.csv", "application/json": "capture.json", "application/geo+json": "capture.json"}
    if media not in names:
        raise HTTPException(415, "Use CSV or JSON for catalog captures.")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > store.MAX_PAYLOAD_BYTES:
            raise HTTPException(413, "The source capture is too large.")
        data.extend(chunk)
    def ingest():
        access.require_ingest(exercise_id, f"catalog:{source_key}")
        return asdict(store.ingest(exercise_id, source_key, names[media], bytes(data), None, "file"))
    return await asyncio.to_thread(ingest)
