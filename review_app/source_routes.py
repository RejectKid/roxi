from typing import Annotated

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import Field

from . import source_catalog as store
from .importer import Slug
from .query_service import check_scope
from .storage import archive_path
from .views import templates

router = APIRouter(tags=["Source catalog"])
CaptureId = Annotated[int, Field(gt=0, le=2**63 - 1)]


@router.get("/exercises/{exercise_id}/sources/catalog.json")
def source_catalog(exercise_id: Slug):
    check_scope(exercise_id)
    return store.catalog(exercise_id)


@router.get("/exercises/{exercise_id}/sources", response_class=HTMLResponse)
def sources_page(request: Request, exercise_id: Slug):
    check_scope(exercise_id)
    return templates.TemplateResponse(request, "sources.html", {
        **store.catalog(exercise_id), "nav_section": "sources",
    })


@router.get("/exercises/{exercise_id}/sources/{source_key}/captures/{capture_id}", response_class=HTMLResponse)
def capture_page(request: Request, exercise_id: Slug, source_key: Slug, capture_id: CaptureId):
    check_scope(exercise_id)
    return templates.TemplateResponse(request, "source_capture.html", {
        **store.capture_details(exercise_id, source_key, capture_id), "nav_section": "sources",
    })


@router.get("/exercises/{exercise_id}/sources/{source_key}/captures/{capture_id}/original", response_class=FileResponse)
def original_capture(exercise_id: Slug, source_key: Slug, capture_id: CaptureId):
    check_scope(exercise_id)
    with store.connect_read() as conn:
        capture = store.find_capture(conn, exercise_id, source_key, capture_id)
    original = archive_path(capture["digest"])
    if not original.is_file():
        raise store.CatalogError(410, "Original source capture is unavailable.")
    return FileResponse(original, media_type="application/octet-stream", filename=capture["original_name"],
                        headers={"X-Content-Type-Options": "nosniff"})
