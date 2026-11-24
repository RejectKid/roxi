import re
from dataclasses import dataclass

from .reported_times import reported_time, time_replaces

CORRECTION = re.compile(r"\bcorrection\b", re.IGNORECASE)
ACKNOWLEDGMENT = re.compile(r"^(?:[^.,!?]{1,48},\s*)?(?:copy|roger|acknowledged)\b", re.IGNORECASE)
BARE_ACKNOWLEDGMENT = re.compile(r"^(?:copy|roger|acknowledged)[.!]?$", re.IGNORECASE)
STATUS = re.compile(
    r"\b(?P<out>oos|out of service|not in service|closed for returns|not open for returns)\b"
    r"|\b(?P<in>in service|open for returns)\b"
    r"|\b(?P<other>(?:is|are|was|were) (?:staged|available|receiving|waiting)|status unknown)\b",
    re.IGNORECASE,
)
LINK_WINDOW_MS = 90_000
CONFLICT_WINDOW_MS = 120_000


@dataclass(frozen=True)
class Proposal:
    kind: str
    subject_id: str | None
    reported_status: str | None
    reported_time_seconds: int | None = None


def subject_entries(glossary: dict) -> list[dict]:
    entries = glossary.get("subjects")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)
            and isinstance(entry.get("subject_id"), str) and isinstance(entry.get("label"), str)]


def subjects(glossary: dict) -> dict[str, str]:
    return {entry["subject_id"]: entry["label"] for entry in subject_entries(glossary)}


def timed_subjects(glossary: dict) -> set[str]:
    return {entry["subject_id"] for entry in subject_entries(glossary) if entry.get("value_type") == "elapsed_time"}


class Extractor:
    def __init__(self, glossary: dict, *, elapsed_times: bool = False):
        owners: dict[str, set[str]] = {}
        for entry in subject_entries(glossary):
            aliases = entry.get("aliases")
            for name in [entry["label"], *(aliases if isinstance(aliases, list) else [])]:
                if isinstance(name, str) and name.strip():
                    owners.setdefault(name.strip().casefold(), set()).add(entry["subject_id"])
        self.aliases = {name: next(iter(ids)) if len(ids) == 1 else None for name, ids in owners.items()}
        self.timed_fields = {
            subject: [alias for alias, owner in self.aliases.items() if owner == subject]
            for subject in timed_subjects(glossary)
        } if elapsed_times else {}
        ordered_aliases = sorted(self.aliases, key=len, reverse=True)
        self.pattern = re.compile(
            r"(?<![\w-])(?:" + "|".join(map(re.escape, ordered_aliases)) + r")(?![\w-])", re.IGNORECASE
        ) if ordered_aliases else None

    def extract(self, body: str) -> Proposal | None:
        status = STATUS.search(body)
        subject_text = body[:status.start()] if status else body
        aliases = list(self.pattern.finditer(subject_text)) if self.pattern else []
        subject = self.aliases.get(aliases[-1].group().casefold()) if aliases else None
        value = {"out": "out_of_service", "in": "in_service", "other": "unknown"}[status.lastgroup] if status else None
        timing = reported_time(body, self.timed_fields.get(subject, []))
        if timing and timing[1]:
            return Proposal("correction", subject, None, timing[0])
        if CORRECTION.search(body):
            return Proposal("correction", subject, value)
        if ACKNOWLEDGMENT.search(body):
            return Proposal("acknowledgment", subject, None)
        if timing:
            return Proposal("time_report", subject, None, timing[0])
        if value:
            return Proposal("status_report", subject, value)
        return None


def earlier(target: dict, event: dict) -> bool:
    source_time = target["capture_offset_ms"]
    event_time = event["capture_offset_ms"]
    if source_time is not None and event_time is not None and source_time != event_time:
        return source_time < event_time
    return target["source_file_id"] == event["source_file_id"] and target["ordinal"] < event["ordinal"]


def link_options(event: dict, events: list[dict]) -> list[dict]:
    if event["kind"] in {"status_report", "time_report"}:
        return []
    options = [target for target in events
               if target["kind"] in {"status_report", "time_report", "correction"}
               and target["state"] != "rejected" and earlier(target, event)]
    if event["kind"] != "correction":
        return options
    if event.get("reported_time_seconds") is not None:
        return [target for target in options if time_replaces(event, target)]
    return [target for target in options if target.get("reported_time_seconds") is None]


def link_candidates(event: dict, options: list[dict]) -> list[int]:
    if event["capture_offset_ms"] is None:
        return []
    nearby = [target for target in options if target["capture_offset_ms"] is not None
              and event["capture_offset_ms"] - target["capture_offset_ms"] <= LINK_WINDOW_MS]
    if event["kind"] == "acknowledgment":
        if event["subject_id"]:
            nearby = [target for target in nearby if target["subject_id"] == event["subject_id"]]
        elif not BARE_ACKNOWLEDGMENT.fullmatch(event["body"].strip()):
            return []
    if event["kind"] == "correction" and event["reported_status"] in {"in_service", "out_of_service"}:
        same_status = [target for target in nearby if target["reported_status"] == event["reported_status"]]
        if same_status:
            nearby = same_status
    return [target["id"] for target in nearby]


def describe_review(events: list[dict]) -> None:
    correction_targets = {event["target_id"] for event in events
                          if event["kind"] == "correction" and event["state"] == "accepted"}
    reports = [event for event in events
               if event["kind"] == "status_report" and event["state"] != "rejected"
               and event["subject_id"] and event["capture_offset_ms"] is not None
               and event["reported_status"] in {"in_service", "out_of_service"}
               and event["id"] not in correction_targets]
    report_ids = {event["id"] for event in reports}
    for event in events:
        choices = link_options(event, events)
        event["options"] = choices
        event["candidates"] = link_candidates(event, choices)
        event["conflicts"] = [other["id"] for other in reports
                              if event["id"] in report_ids and other["subject_id"] == event["subject_id"]
                              and other["reported_status"] != event["reported_status"]
                              and abs(other["capture_offset_ms"] - event["capture_offset_ms"]) <= CONFLICT_WINDOW_MS]
