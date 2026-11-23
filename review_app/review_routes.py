from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveInt, StringConstraints
from typing import Annotated

from . import review_store as store
from . import pilot_access as access
from .views import templates

router = APIRouter(prefix="/exercises/{exercise_id}")
State = Literal["proposed", "accepted", "rejected"]


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: NonNegativeInt


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: NonNegativeInt | None = None


class EventEdit(DraftRequest):
    subject_id: str | None = None
    reported_status: Literal["in_service", "out_of_service", "unknown"] | None = None
    state: State
    target_id: PositiveInt | None = None


class TranscriptEdit(DraftRequest):
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]


def require_local_json(request: Request) -> None:
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(415, "Review changes require JSON.")
    origin = request.headers.get("origin")
    current_origin = f"{request.url.scheme}://{request.url.netloc}"
    if origin is not None and origin != current_origin:
        raise HTTPException(403, "Review changes must originate from the local review app.")


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, exercise_id: str, state: Literal["all", "proposed", "accepted", "rejected"] = "all"):
    access.require(exercise_id, role="review")
    context = store.get_review(exercise_id)
    return templates.TemplateResponse(request, "review.html", {
        **context, "nav_section": "review", "state": state,
        "by_id": {event["id"]: event for event in context["events"]},
    })


@router.post("/review/extract")
def extract(request: Request, exercise_id: str, data: ExtractRequest):
    require_local_json(request)
    access.require(exercise_id, role="review")
    return store.start_or_refresh(exercise_id, data.version)


@router.post("/review/events/{event_id}")
def edit_event(request: Request, exercise_id: str, event_id: PositiveInt, data: EventEdit):
    require_local_json(request)
    access.require(exercise_id, role="review")
    return {"version": store.save_event(exercise_id, event_id, **data.model_dump())}


@router.post("/review/publish")
def publish(request: Request, exercise_id: str, data: DraftRequest):
    require_local_json(request)
    access.require(exercise_id, role="review")
    number = store.publish(exercise_id, data.version)
    return {"number": number, "url": f"/exercises/{exercise_id}/revisions/{number}"}


@router.get("/revisions", response_class=HTMLResponse)
@router.get("/revisions/{number}", response_class=HTMLResponse)
def revisions_page(request: Request, exercise_id: str, number: PositiveInt | None = None):
    access.require(exercise_id)
    context = store.get_revisions(exercise_id, number)
    return templates.TemplateResponse(request, "revisions.html", {
        **context, "nav_section": "revisions",
        "by_id": {event["id"]: event for event in context["events"]},
    })


@router.get("/evidence/{source_id}/{ordinal}", response_class=HTMLResponse)
def evidence_page(request: Request, exercise_id: str, source_id: PositiveInt, ordinal: PositiveInt,
                  revision: PositiveInt | None = None):
    access.require(exercise_id, role="read" if revision else "review")
    context = store.get_evidence(exercise_id, source_id, ordinal, revision)
    return templates.TemplateResponse(request, "evidence.html", {
        **context, "nav_section": "revisions" if revision else "review",
    })


@router.post("/evidence/{source_id}/{ordinal}/transcript")
def edit_transcript(request: Request, exercise_id: str, source_id: PositiveInt, ordinal: PositiveInt,
                    data: TranscriptEdit):
    require_local_json(request)
    access.require(exercise_id, role="review")
    return {"version": store.save_transcript(exercise_id, source_id, ordinal, data.version, data.body)}
