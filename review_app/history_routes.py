from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse

from .history_registry import HistoryError, Version, original_digest
from .history_view import HistoryFilters, view
from .importer import Slug
from .query_service import check_scope
from .storage import archive_path
from .views import templates

router = APIRouter(tags=["Exercise history"])


@router.get("/exercises/{exercise_id}/history/view.json")
def history_view(exercise_id: Slug, filters: Annotated[HistoryFilters, Query()]):
    check_scope(exercise_id)
    return view(exercise_id, filters)


@router.get("/exercises/{exercise_id}/history", response_class=HTMLResponse)
def history_page(request: Request, exercise_id: Slug, filters: Annotated[HistoryFilters, Query()]):
    check_scope(exercise_id)
    context = view(exercise_id, filters)
    points = [
        {"key": row["key"], "label": row["label"], "kind": row["kind"], "position": row["position"]}
        for row in context["observations"] if row["position"]
    ]
    shapes = [feature for layer in context["layers"] for feature in layer["features"]]
    cutoff = {"known_at": context["known_at"].isoformat()}
    if filters.as_of:
        cutoff["as_of"] = filters.as_of.isoformat()
    return templates.TemplateResponse(request, "history.html", {
        **context, "nav_section": "history", "known_filter": filters.known_at,
        "map_data": jsonable_encoder({"points": points, "shapes": shapes}),
        "pinned_url": f"/exercises/{exercise_id}/history?{urlencode(cutoff)}",
    })


def download(digest: str, filename: str):
    path = archive_path(digest)
    if not path.is_file():
        raise HistoryError(410, "The original history evidence is unavailable.")
    return FileResponse(path, filename=filename, media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff"})


@router.get("/exercises/{exercise_id}/history/registry/{version}/original", response_class=FileResponse)
def registry_original(exercise_id: Slug, version: Version):
    check_scope(exercise_id)
    return download(original_digest(exercise_id, version), f"registry-v{version}.json")


@router.get("/exercises/{exercise_id}/history/layers/{layer_key}/{version}/original", response_class=FileResponse)
def layer_original(exercise_id: Slug, layer_key: Slug, version: Version):
    check_scope(exercise_id)
    return download(original_digest(exercise_id, version, layer_key), f"{layer_key}-v{version}.geojson")
