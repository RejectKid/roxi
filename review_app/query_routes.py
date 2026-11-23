from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from . import query_models as model
from . import query_service as service
from .importer import Slug
from .storage import archive_path

ERRORS = {
    401: {"model": model.ErrorResult, "description": "Workspace sign-in or access token required"},
    400: {"model": model.ErrorResult, "description": "Invalid request or cursor"},
    404: {"model": model.ErrorResult, "description": "Published revision or evidence not found"},
    503: {"model": model.ErrorResult, "description": "Database temporarily unavailable"},
    "4XX": {"description": "Other client error"},
}
EVIDENCE_ERRORS = {410: {"model": model.ErrorResult, "description": "Original source unavailable"}}
REVISION_PATH = "/{exercise_id}/revisions/{revision_id}"
EVIDENCE_PATH = REVISION_PATH + "/evidence/{evidence_id}"

router = APIRouter(prefix="/v1/exercises", tags=["Published revisions"], responses=ERRORS)


@router.get("/{exercise_id}/revisions/latest", operation_id="get_exercise_revision")
def get_exercise_revision(exercise_id: Slug) -> model.RevisionResult:
    """Resolve the latest published revision to pin in subsequent requests."""
    return service.get_exercise_revision(model.ExerciseRequest(exercise_id=exercise_id))


@router.get(REVISION_PATH + "/records", operation_id="search_records")
def search_records(exercise_id: Slug, revision_id: model.RevisionId,
                   filters: Annotated[model.RecordFilters, Query()]) -> model.RecordPage:
    """Search transcripts and chat in one published revision."""
    return service.search_records(model.RecordRequest(exercise_id=exercise_id, revision_id=revision_id,
                                                      **filters.model_dump()))


@router.get(REVISION_PATH + "/events", operation_id="list_events")
def list_events(exercise_id: Slug, revision_id: model.RevisionId,
                filters: Annotated[model.EventFilters, Query()]) -> model.EventPage:
    """List reviewed events and their evidence-linked relationships."""
    return service.list_events(model.EventRequest(exercise_id=exercise_id, revision_id=revision_id,
                                                  **filters.model_dump()))


@router.get(EVIDENCE_PATH, operation_id="get_evidence", responses=EVIDENCE_ERRORS)
def get_evidence(exercise_id: Slug, revision_id: model.RevisionId, evidence_id: model.EvidenceId) -> model.EvidenceResult:
    """Resolve an evidence ID against the exact source versions in a published revision."""
    return service.get_evidence(model.EvidenceRequest(exercise_id=exercise_id, revision_id=revision_id,
                                                      evidence_id=evidence_id))


@router.get(EVIDENCE_PATH + "/original", response_class=FileResponse, responses={
    **EVIDENCE_ERRORS,
    200: {"content": {"audio/wav": {}, "text/plain": {}}, "description": "Original archived source"},
    206: {"content": {"audio/wav": {}, "text/plain": {}}, "description": "Requested byte range"},
})
def download_original(exercise_id: Slug, revision_id: model.RevisionId, evidence_id: model.EvidenceId):
    """Open the original recording or chat export after checking revision membership."""
    evidence = service.get_evidence(model.EvidenceRequest(exercise_id=exercise_id, revision_id=revision_id,
                                                          evidence_id=evidence_id))
    media_type = "audio/wav" if evidence.source_kind == "audio" else "text/plain"
    return FileResponse(archive_path(evidence.source_sha256), media_type=media_type,
                        headers={"X-Content-Type-Options": "nosniff"})
