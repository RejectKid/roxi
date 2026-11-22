import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from . import query_models as model
from . import query_service as service
from .chat_types import ChatRequest, ChatResponse, Claim, Verdict
from .chatbot import Answerer
from .review_routes import require_local_json
from .review_store import get_revisions
from .verifier import verify_claim
from .views import templates

router = APIRouter(prefix="/exercises/{exercise_id}/assistant", tags=["Exercise assistant"])


@router.get("", response_class=HTMLResponse)
def assistant_page(request: Request, exercise_id: model.Slug, revision_id: model.RevisionId | None = None):
    service.check_scope(exercise_id)
    context = get_revisions(exercise_id)
    revisions = context["revisions"]
    selected = next((row for row in revisions if row["number"] == revision_id), None) if revision_id else (
        revisions[0] if revisions else None
    )
    if revision_id is not None and selected is None:
        raise service.not_found()
    return templates.TemplateResponse(request, "assistant.html", {
        "exercise": context["exercise"], "revisions": revisions, "selected": selected, "nav_section": "assistant",
    })


def draft_answer(pin: model.RevisionRequest, question: str):
    return Answerer(pin, question).answer()


@router.post("/ask")
async def ask(request: Request, exercise_id: model.Slug, data: ChatRequest) -> ChatResponse:
    require_local_json(request)
    pin = model.RevisionRequest(exercise_id=exercise_id, revision_id=data.revision_id)
    text, claim, parser = await asyncio.to_thread(draft_answer, pin, data.question)
    verdict, citations = await verify_claim(claim)
    return ChatResponse(answer=text, parser=parser, claim=claim, citations=citations, verification=verdict)


@router.post("/verify")
async def verify(request: Request, exercise_id: model.Slug, claim: Claim) -> Verdict:
    require_local_json(request)
    service.check_scope(exercise_id)
    if claim.exercise_id != exercise_id:
        raise service.not_found()
    verdict, _citations = await verify_claim(claim)
    return verdict
