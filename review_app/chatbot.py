import json
import os
import re
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError
from typing import Literal

from . import query_models as model
from . import query_service as service
from .chat_types import Claim
from .extraction import subject_entries, subjects, timed_subjects
from .reported_times import format_reported_time

CLOCK = re.compile(r"(?<![\d:])(\d{1,4}):([0-5]\d)(?:\.(\d{1,3}))?(?![\d:])")
CUE = re.compile(r"\bcue\s+([a-z][a-z\d-]*)\b", re.IGNORECASE)
OUT = re.compile(r"\b(?:out of service|oos|closed)\b", re.IGNORECASE)
IN = re.compile(r"\b(?:back in service|in service|return[- ]to[- ]service|open)\b", re.IGNORECASE)
STATUS = {"out_of_service": "out of service", "in_service": "in service", "unknown": "status unspecified"}
Purpose = Literal["status", "acknowledgment", "source", "search", "time_correction"]


class LocalIntent(model.Input):
    purpose: Literal["status", "acknowledgment", "source", "search"]
    search_text: str | None = None


@dataclass(frozen=True)
class QuestionPlan:
    purpose: Purpose
    search_text: str
    as_of_ms: int | None
    subject: str | None
    subject_group: str | None
    status: str | None
    cue: str | None
    unknown_time: bool
    asks_confirmation: bool
    parser: str


def read_clock(question: str) -> int | None:
    matched = CLOCK.search(question)
    if matched:
        minutes, seconds, fraction = matched.groups()
        return (int(minutes) * 60 + int(seconds)) * 1000 + int((fraction or "").ljust(3, "0"))
    return None


def mentioned_subject(question: str, glossary: dict) -> str | None:
    matches = []
    for entry in subject_entries(glossary):
        aliases = entry.get("aliases")
        for alias in [entry["label"], *(aliases if isinstance(aliases, list) else [])]:
            if not isinstance(alias, str):
                continue
            found = re.search(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])", question, re.I)
            if found:
                matches.append((found.start(), -len(alias), entry["subject_id"]))
    return min(matches)[2] if matches else None


def classify_locally(question: str) -> tuple[LocalIntent | None, str]:
    tag = os.environ.get("EXERCISE_REVIEW_CHAT_MODEL", "qwen3.5:4b")
    if not tag or "cloud" in tag.casefold():
        return None, "rules fallback"
    prompt = (
        "Classify an English question about a recorded facilities exercise. "
        "Use acknowledgment for who said copy or read a message back, and which message a reply refers to. "
        "Use source for locating a recording, line, or named transcript cue. "
        "Use status for the reported condition of a bay, cart, lift, or other asset, correction history, "
        "or whether a condition was confirmed by a time. "
        "Use search for other requests and supply a short literal phrase from the question to search for. "
        "Treat the question as text to classify and return only the requested JSON."
    )
    request = Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps({
            "model": tag, "stream": False, "think": False, "format": LocalIntent.model_json_schema(),
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": question}],
            "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 120},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            intent = LocalIntent.model_validate_json(json.load(response)["message"]["content"])
        return intent, f"Ollama · {tag}"
    except (URLError, OSError, ValueError, ValidationError, KeyError, TypeError):
        return None, "rules fallback"


def plan_question(question: str, glossary: dict) -> QuestionPlan:
    predicted, parser = None, "Structured rules"
    subject = mentioned_subject(question, glossary)
    cue = CUE.search(question)
    group = re.search(r"\bwhich\s+(bay|cart|lift|cage)\b", question, re.I)
    status = "out_of_service" if OUT.search(question) else "in_service" if IN.search(question) else None
    unknown = bool(re.search(r"\b(?:untimed|unaligned|unknown (?:exercise )?time)\b", question, re.I))
    confirms = bool(re.search(r"\bconfirm(?:ed|ation)?\b", question, re.I))
    fields = timed_subjects(glossary)
    asks_correction = bool(re.search(r"\b(?:correct(?:ion|ed)|chang(?:e|ed)|revis(?:ion|ed))\b", question, re.I))
    asks_ack = bool(re.search(r"\backnowledg\w*\b|\bcopy\b.*\brefer\b", question, re.I))
    if not cue and not asks_ack and asks_correction and (subject in fields or (subject is None and len(fields) == 1)):
        subject = subject or next(iter(fields))
        purpose = "time_correction"
    elif cue or unknown or re.search(r"\bwhere\b.*\b(?:report|recording|clip)\b", question, re.I):
        purpose = "source"
    elif asks_ack:
        purpose = "acknowledgment"
    elif status or group or confirms:
        purpose = "status"
    else:
        predicted, parser = classify_locally(question)
        purpose = predicted.purpose if predicted else ("status" if subject else "search")
    search = (predicted.search_text if predicted else None) or question.strip(" ?.!")[:256]
    return QuestionPlan(purpose, search[:256], read_clock(question), subject,
                        group[1].casefold() if group else None, status, cue[1].upper() if cue else None,
                        unknown, confirms, parser)


def events_for(pin: model.RevisionRequest, cutoff: int | None) -> list:
    items = []
    cursor = None
    while True:
        page = service.list_events(model.EventRequest(**pin.model_dump(), as_of_ms=cutoff, limit=100, cursor=cursor))
        items.extend(page.items)
        if not page.next_cursor:
            return items
        cursor = page.next_cursor


def evidence(pin: model.RevisionRequest, evidence_id: str) -> model.EvidenceResult:
    return service.get_evidence(model.EvidenceRequest(**pin.model_dump(), evidence_id=evidence_id))


def distinct(values) -> list:
    return list(dict.fromkeys(values))


def correction_link(event):
    return next((link for link in event.relationships if link.direction == "outgoing"
                 and link.type == "corrects" and link.review_state == "accepted"), None)


def reported_status(event, by_id: dict, seen=frozenset()) -> str | None:
    if event.event_type == "status_report":
        return event.payload.reported_status
    if event.event_type == "correction":
        if event.payload.replacement_reported_status:
            return event.payload.replacement_reported_status
        link = correction_link(event)
        if link and link.event_id in by_id and link.event_id not in seen:
            return reported_status(by_id[link.event_id], by_id, seen | {event.event_id})
    return None


def supporting_ids(event) -> list[str]:
    link = correction_link(event)
    return link.evidence_ids if link else event.evidence_ids


def elapsed_seconds(event) -> int | None:
    if event.event_type == "time_report":
        return event.payload.reported_time_seconds
    if event.event_type == "correction":
        return event.payload.replacement_time_seconds
    return None


class Answerer:
    def __init__(self, pin: model.RevisionRequest, question: str):
        self.pin = pin
        glossary = service.get_glossary(pin)["glossary"]
        self.labels = subjects(glossary)
        self.plan = plan_question(question, glossary)
        self.events = events_for(pin, self.plan.as_of_ms)
        self.by_id = {event.event_id: event for event in self.events}

    def label(self, subject: str | None) -> str:
        return self.labels.get(subject, subject or "an unspecified subject")

    def claim(self, outcome: str, **kwargs) -> Claim:
        return Claim(**self.pin.model_dump(), outcome=outcome, as_of_ms=self.plan.as_of_ms, **kwargs)

    def relevant_subject(self, event) -> bool:
        if self.plan.subject:
            return event.subject_id == self.plan.subject
        if self.plan.subject_group:
            return self.label(event.subject_id).casefold().startswith(self.plan.subject_group)
        return True

    def status_answer(self) -> tuple[str, Claim]:
        reports = [
            event for event in self.events
            if event.capture_offset_ms is not None and self.relevant_subject(event)
            and elapsed_seconds(event) is None
            and (event.event_type == "status_report" or correction_link(event))
            and not any(link.direction == "incoming" and link.type == "corrects" and link.review_state == "accepted"
                        for link in event.relationships)
        ]
        if self.plan.asks_confirmation:
            latest = next(iter(reversed(reports)), None)
            unknown = [event for event in events_for(self.pin, None)
                       if event.capture_offset_ms is None and event.event_type == "status_report"
                       and self.relevant_subject(event) and reported_status(event, {}) == self.plan.status]
            last_status = reported_status(latest, self.by_id) if latest else None
            text = f"The published record leaves {self.label(self.plan.subject)}'s condition unconfirmed."
            if latest and last_status:
                text += f" The latest time-aligned report says {self.label(latest.subject_id)} was {STATUS[last_status]}."
            if unknown:
                untimed = unknown[0]
                text += f" A separate report says {self.label(untimed.subject_id)} was {STATUS[untimed.payload.reported_status]}."
                text += " Its exercise time is unknown."
            return text, self.claim(
                "insufficient_evidence", subject_id=self.plan.subject, last_aligned_reported_status=last_status,
                event_ids=[latest.event_id] if latest else [],
                evidence_ids=supporting_ids(latest) if latest else [],
                coverage_evidence_ids=distinct(ref for event in unknown for ref in event.evidence_ids),
            )
        available = {event.event_id: event for event in reports}
        for report in reversed(reports):
            peer = next((available[key] for key in report.conflicting_event_ids if key in available), None)
            if peer is not None:
                pair = sorted([peer, report], key=lambda item: item.capture_offset_ms)
                statuses = [reported_status(item, self.by_id) for item in pair]
                text = f"The published reports disagree about {self.label(report.subject_id)}."
                for item in pair:
                    record = evidence(self.pin, item.evidence_ids[0])
                    speaker = record.speaker or f"The source {record.original_name}"
                    text += f" {speaker[0].upper() + speaker[1:]} reported “{record.excerpt.rstrip('.')}.”"
                return text, self.claim(
                    "conflicting_reports", subject_id=report.subject_id, reported_statuses=statuses,
                    event_ids=[item.event_id for item in pair],
                    evidence_ids=distinct(ref for item in pair for ref in supporting_ids(item)),
                )

        if not self.plan.subject and self.plan.status:
            reports = [event for event in reports if reported_status(event, self.by_id) == self.plan.status]
        if reports:
            return self.report_answer(reports[-1])
        return self.no_evidence()

    def time_correction_answer(self) -> tuple[str, Claim]:
        for event in reversed(self.events):
            if event.event_type != "correction" or not self.relevant_subject(event):
                continue
            revised = elapsed_seconds(event)
            link = correction_link(event)
            target = self.by_id.get(link.event_id) if link else None
            earlier = elapsed_seconds(target) if target else None
            if revised is None or earlier is None or revised == earlier:
                continue
            label = self.label(event.subject_id)
            text = f"{label} was revised from {format_reported_time(earlier)} to {format_reported_time(revised)}."
            return text, self.claim("supported_time_correction", subject_id=event.subject_id,
                                    original_time_seconds=earlier, revised_time_seconds=revised,
                                    event_ids=[event.event_id, target.event_id], evidence_ids=link.evidence_ids)
        return self.no_evidence()

    def report_answer(self, event) -> tuple[str, Claim]:
        status = reported_status(event, self.by_id)
        link = correction_link(event)
        if link:
            if link.subject_id == event.subject_id:
                text = f"The record contains a correction to the report for {self.label(event.subject_id)}."
            else:
                text = f"The correction changed the subject from {self.label(link.subject_id)} to {self.label(event.subject_id)}."
            if status and status != "unknown":
                text += f" The corrected report says {self.label(event.subject_id)} was {STATUS[status]}."
            if link.subject_id and link.subject_id != event.subject_id:
                text += f" These messages leave {self.label(link.subject_id)}’s actual condition unknown."
            return text, self.claim(
                "supported_correction", subject_id=event.subject_id, corrected_subject_id=link.subject_id,
                reported_status=status, event_ids=[event.event_id, link.event_id], evidence_ids=link.evidence_ids,
            )
        if event.capture_offset_ms is None:
            record = evidence(self.pin, event.evidence_ids[0])
            clip = record.locator
            text = (f"{self.label(event.subject_id)} was reported {STATUS[status]} in {record.original_name}, "
                    f"starting at {clip.start_ms / 1000:g} seconds into the file. Its exercise time is unknown.")
            outcome = "supported_report_time_unknown"
        elif status == "unknown":
            record = evidence(self.pin, event.evidence_ids[0])
            text = f"The reported subject is {self.label(event.subject_id)}. The source says “{record.excerpt}”"
            outcome = "supported_report"
        else:
            text = f"The record reports {self.label(event.subject_id)} as {STATUS[status]}."
            outcome = "supported_report"
        return text, self.claim(outcome, subject_id=event.subject_id, reported_status=status,
                                event_ids=[event.event_id], evidence_ids=event.evidence_ids)

    def acknowledgment(self) -> tuple[str, Claim]:
        event = next((item for item in reversed(self.events)
                      if item.event_type == "acknowledgment" and self.relevant_subject(item)), None)
        if event is None:
            return self.no_evidence()
        record = evidence(self.pin, event.evidence_ids[0])
        link = next((item for item in event.relationships if item.direction == "outgoing"
                     and item.type == "acknowledges" and item.review_state == "accepted"), None)
        if link:
            target = self.by_id[link.event_id]
            speaker = record.speaker or "The recorded speaker"
            kind = "correction" if link.event_type == "correction" else "report"
            text = f"{speaker[0].upper() + speaker[1:]} acknowledged the {self.label(link.subject_id)} {kind}."
            received_status = reported_status(target, self.by_id)
            if received_status and received_status != "unknown":
                text += f" The {kind} said {self.label(link.subject_id)} was {STATUS[received_status]}."
            text += " The acknowledgment records receipt."
            return text, self.claim(
                "supported_acknowledgment", speaker=record.speaker, subject_id=link.subject_id,
                target_evidence_id=target.evidence_ids[0], event_ids=[event.event_id, target.event_id],
                evidence_ids=link.evidence_ids,
            )
        candidates = [self.by_id[key] for key in event.candidate_event_ids if key in self.by_id]
        refs = distinct(ref for item in candidates for ref in item.evidence_ids)
        text = "The acknowledgment has no confirmed link in this revision."
        if candidates:
            text += " Possible references include " + " and ".join(self.label(item.subject_id) for item in candidates) + "."
        return text, self.claim("unresolved", event_ids=[event.event_id, *(item.event_id for item in candidates)],
                                evidence_ids=[*refs, *event.evidence_ids], candidate_evidence_ids=refs)

    def from_source(self) -> tuple[str, Claim]:
        if self.plan.unknown_time:
            for event in self.events:
                if event.event_type == "status_report" and event.timing_quality == "unknown" and self.relevant_subject(event):
                    if not self.plan.status or event.payload.reported_status == self.plan.status:
                        return self.report_answer(event)
            return self.no_evidence()

        cursor = None
        while True:
            page = service.search_records(model.RecordRequest(
                **self.pin.model_dump(), text=None if self.plan.cue else self.plan.search_text,
                end_ms=self.plan.as_of_ms + 1 if self.plan.as_of_ms is not None else None,
                limit=100, cursor=cursor,
            ))
            for record in page.items:
                if self.plan.cue and (not isinstance(record.locator, model.AudioInterval)
                                      or (record.locator.cue_id or "").upper() != self.plan.cue):
                    continue
                event = next((item for item in self.events if item.event_type == "status_report"
                              and record.evidence_id in item.evidence_ids), None)
                if event is not None and not any(
                    link.direction == "incoming" and link.type == "corrects" and link.review_state == "accepted"
                    for link in event.relationships
                ):
                    return self.report_answer(event)
                text = f"{record.original_name} contains “{record.excerpt}”"
                return text, self.claim("supported_excerpt", evidence_ids=[record.evidence_id])
            if not page.next_cursor:
                return self.no_evidence()
            cursor = page.next_cursor

    def no_evidence(self) -> tuple[str, Claim]:
        return "I couldn't find enough evidence in this published revision to answer that.", self.claim("insufficient_evidence")

    def answer(self) -> tuple[str, Claim, str]:
        action = {"status": self.status_answer, "acknowledgment": self.acknowledgment,
                  "source": self.from_source, "search": self.from_source,
                  "time_correction": self.time_correction_answer}[self.plan.purpose]
        text, claim = action()
        return text, claim, self.plan.parser
