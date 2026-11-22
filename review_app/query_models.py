from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .importer import Slug

MAX_BIGINT = 2**63 - 1
MAX_INT = 2**31 - 1
RevisionId = Annotated[int, Field(ge=1, le=MAX_INT)]
Offset = Annotated[int, Field(ge=0, le=MAX_BIGINT)]
EvidenceId = Annotated[str, StringConstraints(
    pattern=r"^s[1-9][0-9]{0,18}-r[1-9][0-9]{0,9}(?:-t[a-f0-9]{64})?$",
    max_length=98,
)]
EventType = Literal["status_report", "acknowledgment", "correction"]
ReviewState = Literal["proposed", "accepted", "rejected"]
ReportedStatus = Literal["in_service", "out_of_service", "unknown"]
SourceKind = Literal["audio", "chat"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExerciseRequest(Input):
    exercise_id: Slug


class RevisionRequest(ExerciseRequest):
    revision_id: RevisionId


class PageFilters(Input):
    start_ms: Offset | None = None
    end_ms: Offset | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 25
    cursor: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None


class RecordFilters(PageFilters):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)] | None = None
    channel: Slug | None = None


class EventFilters(PageFilters):
    subject_id: Annotated[str, StringConstraints(min_length=1, max_length=160)] | None = None
    event_types: Annotated[list[EventType], Field(max_length=3)] = Field(default_factory=list)
    as_of_ms: Offset | None = None
    review_state: Literal["all", "proposed", "accepted", "rejected"] = "accepted"


class RecordRequest(RecordFilters, RevisionRequest):
    pass


class EventRequest(EventFilters, RevisionRequest):
    pass


class EvidenceRequest(RevisionRequest):
    evidence_id: EvidenceId


class Coverage(BaseModel):
    aligned_sources: int
    unaligned_sources: int
    known_gaps: list[str]
    unknown_time_excluded: int = 0


class RevisionRef(BaseModel):
    exercise_id: str
    revision_id: int


class RevisionResult(RevisionRef):
    exercise_name: str
    published_at: datetime
    coverage: Coverage


class AudioInterval(BaseModel):
    kind: Literal["audio_interval"] = "audio_interval"
    start_ms: int
    end_ms: int
    cue_id: str | None


class ChatLines(BaseModel):
    kind: Literal["chat_lines"] = "chat_lines"
    line_start: int
    line_end: int


class RecordItem(BaseModel):
    evidence_id: EvidenceId
    source_file_id: int
    channel: str
    source_kind: SourceKind
    original_name: str
    speaker: str | None
    capture_offset_ms: int | None
    timing_quality: Literal["exact", "unknown"]
    excerpt: str
    locator: Annotated[AudioInterval | ChatLines, Field(discriminator="kind")]


class RecordPage(RevisionRef):
    items: list[RecordItem]
    coverage: Coverage
    next_cursor: str | None


class Relationship(BaseModel):
    type: Literal["acknowledges", "corrects"]
    direction: Literal["incoming", "outgoing"]
    event_id: int
    event_type: EventType
    subject_id: str | None
    review_state: ReviewState
    evidence_ids: list[EvidenceId]


class EventItem(BaseModel):
    event_id: int
    subject_id: str | None
    capture_offset_ms: int | None
    timing_quality: Literal["exact", "unknown"]
    review_state: ReviewState
    evidence_ids: list[EvidenceId]
    relationships: list[Relationship]
    conflicting_event_ids: list[int]


class ReportPayload(BaseModel):
    reported_status: ReportedStatus


class AcknowledgmentPayload(BaseModel):
    text: str


class CorrectionPayload(BaseModel):
    replacement_subject_id: str | None
    replacement_reported_status: ReportedStatus | None


class ReportEvent(EventItem):
    event_type: Literal["status_report"] = "status_report"
    payload: ReportPayload


class AcknowledgmentEvent(EventItem):
    event_type: Literal["acknowledgment"] = "acknowledgment"
    payload: AcknowledgmentPayload


class CorrectionEvent(EventItem):
    event_type: Literal["correction"] = "correction"
    payload: CorrectionPayload


PublishedEvent = Annotated[ReportEvent | AcknowledgmentEvent | CorrectionEvent, Field(discriminator="event_type")]


class EventPage(RevisionRef):
    items: list[PublishedEvent]
    coverage: Coverage
    next_cursor: str | None


class EvidenceResult(RecordItem, RevisionRef):
    original_excerpt: str
    original_text: str
    source_sha256: str
    source_byte_size: int
    original_path: str
    playback_path: str | None


class ErrorInfo(BaseModel):
    code: Literal["invalid_argument", "not_found", "source_unavailable", "temporarily_unavailable"]
    message: str
    request_id: str
    retryable: bool


class ErrorResult(BaseModel):
    error: ErrorInfo
