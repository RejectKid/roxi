from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from .query_models import EvidenceId, EvidenceResult, Input, Offset, ReportedStatus, ReportedTime, RevisionRequest

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class ChatRequest(Input):
    revision_id: Annotated[int, Field(ge=1, le=2**31 - 1)]
    question: Question


class Claim(RevisionRequest):
    outcome: Literal[
        "supported_report", "supported_correction", "supported_acknowledgment", "unresolved",
        "conflicting_reports", "insufficient_evidence", "supported_report_time_unknown", "supported_excerpt",
        "supported_time_correction",
    ] = "insufficient_evidence"
    as_of_ms: Offset | None = None
    subject_id: str | None = None
    reported_status: ReportedStatus | None = None
    corrected_subject_id: str | None = None
    original_time_seconds: ReportedTime | None = None
    revised_time_seconds: ReportedTime | None = None
    speaker: str | None = None
    target_evidence_id: EvidenceId | None = None
    candidate_evidence_ids: list[EvidenceId] = Field(default_factory=list, max_length=30)
    reported_statuses: list[ReportedStatus] = Field(default_factory=list, max_length=3)
    last_aligned_reported_status: ReportedStatus | None = None
    event_ids: list[int] = Field(default_factory=list, max_length=30)
    evidence_ids: list[EvidenceId] = Field(default_factory=list, max_length=30)
    coverage_evidence_ids: list[EvidenceId] = Field(default_factory=list, max_length=30)


class Verdict(Input):
    status: Literal["supported", "contradicted", "insufficient_evidence"]
    reason: str
    evidence_ids: list[EvidenceId] = Field(default_factory=list)


class ChatResponse(Input):
    answer: str
    parser: str
    claim: Claim
    citations: list[EvidenceResult]
    verification: Verdict
