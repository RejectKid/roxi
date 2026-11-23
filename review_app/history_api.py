import asyncio
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from . import history_models as model
from . import history_query as service
from . import pilot_access as access
from .history_changes import list_changes, resume_token
from .history_view import HistoryFilters, view
from .history_voice import receive_question
from .importer import Slug
from .query_routes import ERRORS
from .query_service import QueryError
from .review_routes import require_local_json
from .views import templates

router = APIRouter(tags=["History queries"])


@router.get("/v1/history/query", operation_id="query_history", responses=ERRORS)
def query_history(request: Annotated[model.HistoryQuery, Query()]) -> model.HistoryResult:
    """Return source-backed history, differences, conflicts, or overdue observations for fixed cutoffs."""
    return service.query_history(request)


@router.get("/v1/history/evidence", operation_id="get_history_evidence", responses=ERRORS)
def history_evidence(request: Annotated[model.EvidenceRequest, Query()]) -> model.OpenHistoryEvidence:
    """Resolve a history citation against the same cutoffs and verify its exact original record."""
    return service.open_evidence(request)


@router.get("/v1/history/changes", operation_id="list_history_changes", responses=ERRORS)
def history_changes(request: Annotated[model.ChangeRequest, Query()]) -> model.ChangePage:
    """Read committed change IDs after an exercise-specific resume token."""
    return list_changes(request)


@router.get("/exercises/{exercise_id}/history/changes/stream", response_class=StreamingResponse,
            responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}})
async def history_stream(
    exercise_id: Slug, after: model.ResumeToken | None = None, last_event_id: Annotated[model.ResumeToken | None, Header()] = None,
):
    """Stream committed history changes, starting after Last-Event-ID or the supplied after token."""
    initial = await asyncio.to_thread(list_changes, model.ChangeRequest(exercise_id=exercise_id,
                                                                       after=last_event_id or after, limit=64))

    async def events():
        page, idle = initial, 0
        yield ": connected\n\n"
        while True:
            for event in page.changes:
                yield f"id: {resume_token(exercise_id, event.sequence)}\nevent: change\ndata: {event.model_dump_json()}\n\n"
            if page.changes:
                idle = 0
            if not page.has_more:
                await asyncio.sleep(0.5)
                idle += 1
                if idle >= 30:
                    yield ": heartbeat\n\n"
                    idle = 0
            try:
                page = await asyncio.to_thread(list_changes, model.ChangeRequest(exercise_id=exercise_id,
                                                                                 after=page.resume_token, limit=64))
            except QueryError:
                return
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.get("/exercises/{exercise_id}/history/assistant", response_class=HTMLResponse)
def assistant_page(request: Request, exercise_id: Slug, filters: Annotated[HistoryFilters, Query()]):
    grant = access.require(exercise_id, full=False)
    snapshot = view(exercise_id, filters, grant.sources)
    return templates.TemplateResponse(request, "history_assistant.html", {
        "exercise": snapshot["exercise"], "known_at": snapshot["known_at"], "as_of": snapshot["as_of"],
        "nav_section": "assistant", "grant": grant,
    })


@router.post("/exercises/{exercise_id}/history/answer")
def history_answer(request: Request, exercise_id: Slug, question: model.QuestionRequest) -> model.HistoryAnswer:
    require_local_json(request)
    return service.answer(exercise_id, question)


@router.post("/exercises/{exercise_id}/history/voice")
async def history_voice(request: Request, exercise_id: Slug) -> model.VoiceTranscript:
    await asyncio.to_thread(access.require, exercise_id, full=False)
    return model.VoiceTranscript(question=await receive_question(request))
