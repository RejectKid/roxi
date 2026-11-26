from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from .chat_types import Question
from .query_models import Cursor, Input, ReportedStatus, Slug

Mode = Literal["snapshot", "changes", "conflicts", "overdue"]
ObservationKey = Annotated[str, StringConstraints(pattern=r"^(chat-[1-9][0-9]*|capture-[1-9][0-9]*-[1-9][0-9]*-[1-9][0-9]*)$", max_length=80)]
PageSize = Annotated[int, Field(ge=1, le=100)]
ResumeToken = Annotated[str, StringConstraints(pattern=r"^h1\.[a-z0-9-]+\.[0-9]+$", max_length=125)]


class HistoryPin(Input):
    exercise_id: Slug
    known_at: AwareDatetime
    as_of: AwareDatetime | None = None

    @field_validator("known_at", "as_of")
    @classmethod
    def utc(cls, value):
        return value.astimezone(UTC) if value is not None else None


class HistoryQuery(HistoryPin):
    mode: Mode = "snapshot"
    baseline_known_at: AwareDatetime | None = None
    entity_id: Slug | None = None
    limit: PageSize = 25
    cursor: Cursor | None = None

    @field_validator("baseline_known_at")
    @classmethod
    def baseline_utc(cls, value):
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def compare_cutoffs(self):
        if self.baseline_known_at and self.baseline_known_at >= self.known_at:
            raise ValueError("The baseline must precede the known-by cutoff.")
        return self


class EvidenceRequest(HistoryPin):
    key: ObservationKey


class HistoryLocator(Input):
    kind: Literal["csv_row", "json_pointer", "message"]
    value: int | str
    version: int | None = None


class AudioInterval(Input):
    kind: Literal["audio_interval"]
    value: str
    start_ms: Annotated[int, Field(ge=0)]
    end_ms: Annotated[int, Field(gt=0)]


class ReviewedIdentity(Input):
    entity_id: Slug
    label: str
    external_id: str
    registry_version: int
    registry_url: str
    registry_sha256: str


class HistoryCitation(HistoryPin):
    key: ObservationKey
    source: str
    locator: Annotated[HistoryLocator | AudioInterval, Field(discriminator="kind")]
    detail_url: str
    original_url: str
    parser_version: int | None = None
    sha256: str
    identity: ReviewedIdentity | None = None
    envelope_sha256: str | None = None


class OpenHistoryEvidence(HistoryCitation):
    original_record: dict[str, Any]


class HistoryRecord(Input):
    key: ObservationKey
    source: str
    record_id: str
    kind: str
    state: Literal["observation", "proposed"]
    observed_at: datetime | None
    received_at: datetime
    available_at: datetime
    late: bool
    text: str | None = None
    medium: Literal["chat", "radio"] | None = None
    sender: str | None = None
    transcription_model: str | None = None
    input_version: int | None = None
    reported_status: ReportedStatus | None = None
    reported_time_seconds: int | None = None
    telemetry_status: str | None = None
    position: tuple[float, float] | None = None
    external_id: str | None
    entity_id: Slug | None
    identity_status: Literal["matched", "ambiguous", "unresolved"]
    candidate_entity_ids: list[Slug]
    candidate_target_keys: list[ObservationKey]


class HistoryFact(Input):
    kind: Literal["observed", "added", "updated", "removed", "conflict", "overdue"]
    subject: str
    observed_at: datetime | None = None
    text: str
    citations: list[HistoryCitation]
    records: list[HistoryRecord]


class HistoryResult(HistoryPin):
    mode: Mode
    baseline_known_at: datetime | None = None
    facts: list[HistoryFact]
    gaps: list[str]
    next_cursor: str | None = None


class QuestionRequest(Input):
    question: Question
    known_at: AwareDatetime
    as_of: AwareDatetime | None = None
    baseline_known_at: AwareDatetime | None = None


class HistoryAnswerRequest(QuestionRequest):
    exercise_id: Slug


class HistoryAnswer(Input):
    answer: str
    result: HistoryResult


class VoiceTranscript(Input):
    question: Question


class ChangeRequest(Input):
    exercise_id: Slug
    after: ResumeToken | None = None
    limit: PageSize = 25


class HistoryChange(Input):
    sequence: Annotated[int, Field(ge=1)]
    event: Literal["catalog.derived", "chat.classified", "registry.published", "layer.published"]
    ref: str
    version: Annotated[int, Field(ge=1)]
    available_at: datetime


class ChangePage(Input):
    exercise_id: Slug
    changes: list[HistoryChange]
    resume_token: ResumeToken
    has_more: bool
