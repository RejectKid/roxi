import asyncio
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, ValidationError

from . import channel_capture as channel
from . import pilot_access as access
from . import query_service as query
from .importer import Slug
from .review_routes import require_local_json
from .source_adapters import MAX_PAYLOAD_BYTES
from .source_catalog import CatalogError
from .storage import archive_path

router = APIRouter(prefix="/v1/exercises/{exercise_id}/channels", tags=["Civilian communications"])
CaptureId = Annotated[int, Field(gt=0, le=2**63 - 1)]
CAPTURE_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": {
            "oneOf": [channel.ChannelMessage.model_json_schema(), channel.CompletedClip.model_json_schema()],
        }}},
    },
}


def capture_detail(exercise_id: str, source_key: str, capture_id: int) -> dict:
    access.require(exercise_id, full=False, source=f"catalog:{source_key}")
    try:
        return channel.lookup(exercise_id, source_key, capture_id)
    except CatalogError as error:
        raise query.not_found(error.detail) from None


@router.post("/{source_key}/captures", status_code=202, response_model=channel.ChannelReceipt,
             responses={200: {"model": channel.ChannelReceipt, "description": "That event version was already received"}},
             openapi_extra=CAPTURE_BODY)
async def ingest_capture(request: Request, exercise_id: Slug, source_key: Slug):
    """Save a versioned channel message or completed radio clip via an ingestion token."""
    require_local_json(request)
    await asyncio.to_thread(access.require_ingest, exercise_id, f"catalog:{source_key}")
    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > MAX_PAYLOAD_BYTES:
            raise query.QueryError(413, "invalid_argument", "The channel capture exceeds the 2 MiB limit.")
        data.extend(part)
    try:
        item = channel.CAPTURE.validate_json(data)
    except ValidationError:
        raise query.invalid("Supply a valid channel chat or completed radio envelope.") from None

    def receive():
        access.require_ingest(exercise_id, f"catalog:{source_key}")
        return channel.receive(exercise_id, source_key, item, bytes(data))

    try:
        receipt = await asyncio.to_thread(receive)
    except CatalogError as error:
        code = "not_found" if error.status == 404 else "invalid_argument"
        raise query.QueryError(error.status, code, error.detail) from None
    return JSONResponse(asdict(receipt), status_code=200 if receipt.duplicate else 202)


@router.get("/{source_key}/captures/{capture_id}", response_model=channel.ChannelReceipt)
def capture_status(exercise_id: Slug, source_key: Slug, capture_id: CaptureId) -> channel.ChannelReceipt:
    """Check whether a durable capture is waiting for transcription or has a derivation."""
    row = capture_detail(exercise_id, source_key, capture_id)
    return channel.ChannelReceipt(capture_id, row["state"] or "pending")


@router.get("/{source_key}/captures/{capture_id}/audio", response_class=FileResponse)
def original_audio(exercise_id: Slug, source_key: Slug, capture_id: CaptureId) -> Response:
    """Return the original PCM WAV with standard HTTP Range support."""
    row = capture_detail(exercise_id, source_key, capture_id)
    if row["audio_digest"] is None:
        raise query.not_found("This capture has no audio.")
    path = archive_path(row["audio_digest"])
    if not path.is_file():
        raise query.QueryError(410, "source_unavailable", "The archived audio is unavailable.")
    return FileResponse(path, media_type="audio/wav", headers={"X-Content-Type-Options": "nosniff"})
