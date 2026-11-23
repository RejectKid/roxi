import json
import re

from mcp import Client

from .chat_types import Claim, Verdict
from .extraction import subject_entries
from .mcp_server import create_server
from .query_models import EventPage, EvidenceResult

STATUS_PATTERNS = {
    "out_of_service": re.compile(r"\b(?:oos|out of service|not in service|closed for returns)\b", re.I),
    "in_service": re.compile(r"\b(?:in service|open for returns)\b", re.I),
}


class MissingEvidence(Exception):
    pass


async def read_tool(client: Client, tool: str, arguments: dict) -> dict:
    result = await client.call_tool(tool, arguments)
    if result.is_error or not isinstance(result.structured_content, dict):
        raise MissingEvidence(f"The independent MCP lookup could not resolve {tool}.")
    return result.structured_content


async def reviewed_events(client: Client, pin: dict, cutoff: int | None) -> dict:
    events = {}
    cursor = None
    while True:
        body = await read_tool(client, "list_events", {**pin, "limit": 100, "cursor": cursor, "as_of_ms": cutoff})
        page = EventPage.model_validate(body)
        events.update({item.event_id: item for item in page.items})
        cursor = page.next_cursor
        if not cursor:
            return events


def aliases_for(glossary: dict) -> dict[str, list[str]]:
    result = {}
    for entry in subject_entries(glossary):
        aliases = entry.get("aliases")
        alternatives = aliases if isinstance(aliases, list) else []
        result[entry["subject_id"]] = [entry["label"], *(name for name in alternatives if isinstance(name, str))]
    return result


def contains_subject(excerpt: str, subject: str | None, aliases: dict[str, list[str]]) -> bool:
    return subject is None or any(re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", excerpt, re.I)
                                  for name in aliases.get(subject, []))


def contains_status(excerpt: str, status: str | None) -> bool:
    if status in (None, "unknown"):
        return True
    if status == "in_service" and STATUS_PATTERNS["out_of_service"].search(excerpt):
        return False
    return bool(STATUS_PATTERNS[status].search(excerpt))


def relates(event, kind: str, target: int | None = None):
    return next((link for link in event.relationships if link.direction == "outgoing" and link.type == kind
                 and link.review_state == "accepted" and (target is None or link.event_id == target)), None)


def superseded(event) -> bool:
    return any(link.direction == "incoming" and link.type == "corrects" and link.review_state == "accepted"
               for link in event.relationships)


def event_condition(event, events: dict, visited=frozenset()) -> str | None:
    if event.event_type == "status_report":
        return event.payload.reported_status
    if event.event_type != "correction" or event.event_id in visited:
        return None
    if event.payload.replacement_reported_status:
        return event.payload.replacement_reported_status
    link = relates(event, "corrects")
    return (event_condition(events[link.event_id], events, visited | {event.event_id})
            if link and link.event_id in events else None)


def source_support(event, claims: dict[str, EvidenceResult], aliases: dict[str, list[str]]) -> bool:
    cited = [claims[key].excerpt for key in event.evidence_ids if key in claims]
    if event.event_type == "acknowledgment":
        return any(re.search(r"\b(?:copy|roger|acknowledged)\b", text, re.I) for text in cited)
    if event.event_type == "correction":
        return any(re.search(r"\bcorrection\b", text, re.I)
                   and contains_subject(text, event.subject_id, aliases)
                   and contains_status(text, event.payload.replacement_reported_status) for text in cited)
    return any(contains_subject(text, event.subject_id, aliases)
               and contains_status(text, event.payload.reported_status) for text in cited)


def evaluate_claim(claim: Claim, main: dict[str, EvidenceResult], coverage: dict[str, EvidenceResult],
                   events: dict, aliases: dict[str, list[str]]) -> Verdict:
    def verdict(status, reason):
        return Verdict(status=status, reason=reason, evidence_ids=[*main, *coverage])

    if not main:
        return verdict("insufficient_evidence", "The draft supplies no primary evidence to verify.")
    if claim.as_of_ms is not None and any(
        item.capture_offset_ms is None or item.capture_offset_ms > claim.as_of_ms for item in main.values()
    ):
        return verdict("contradicted", "A primary citation falls outside the claimed exercise-time cutoff.")
    if any(item.capture_offset_ms is not None for item in coverage.values()):
        return verdict("contradicted", "The coverage citations were marked untimed but include aligned evidence.")
    if claim.outcome == "supported_excerpt" and not claim.event_ids:
        return verdict("supported", "The quoted excerpt resolves in the pinned revision and its original source is available.")

    cited_events = [events.get(key) for key in claim.event_ids]
    if not cited_events or None in cited_events:
        return verdict("insufficient_evidence", "A referenced event is missing from the accepted published record.")
    if any(not source_support(event, main, aliases) for event in cited_events):
        return verdict("contradicted", "A cited excerpt disagrees with the event's stated subject, status, or message type.")

    first = cited_events[0]
    outcome = claim.outcome
    valid = False
    if outcome in {"supported_report", "supported_report_time_unknown"}:
        valid = (len(cited_events) == 1 and first.event_type == "status_report"
                 and first.subject_id == claim.subject_id and event_condition(first, events) == claim.reported_status
                 and (first.capture_offset_ms is None) == (outcome == "supported_report_time_unknown")
                 and not superseded(first))
    elif outcome == "supported_correction" and len(cited_events) == 2:
        earlier = cited_events[1]
        link = relates(first, "corrects", earlier.event_id)
        valid = bool(first.event_type == "correction" and link and set(link.evidence_ids) <= main.keys()
                     and claim.corrected_subject_id == earlier.subject_id and claim.subject_id == first.subject_id
                     and claim.reported_status == event_condition(first, events) and not superseded(first))
    elif outcome == "supported_acknowledgment" and len(cited_events) == 2:
        target = cited_events[1]
        link = relates(first, "acknowledges", target.event_id)
        valid = bool(first.event_type == "acknowledgment" and link and set(link.evidence_ids) <= main.keys()
                     and claim.subject_id == target.subject_id and claim.target_evidence_id in target.evidence_ids
                     and any(main[key].speaker == claim.speaker for key in first.evidence_ids if key in main))
    elif outcome == "unresolved":
        candidate_refs = {key for event_id in first.candidate_event_ids if event_id in events
                          for key in events[event_id].evidence_ids}
        valid = (first.event_type == "acknowledgment" and not relates(first, "acknowledges")
                 and candidate_refs == set(claim.candidate_evidence_ids)
                 and candidate_refs <= main.keys())
    elif outcome == "conflicting_reports" and len(cited_events) == 2:
        second = cited_events[1]
        values = {event_condition(first, events), event_condition(second, events)}
        valid = (first.subject_id == second.subject_id == claim.subject_id and len(values) == 2
                 and values == set(claim.reported_statuses) and second.event_id in first.conflicting_event_ids)
    elif outcome == "insufficient_evidence":
        timed_reports = [event for event in events.values() if event.subject_id == claim.subject_id
                         and event.capture_offset_ms is not None
                         and (event.event_type == "status_report" or relates(event, "corrects"))
                         and not superseded(event)]
        latest = max(timed_reports, key=lambda event: (event.capture_offset_ms, event.event_id), default=None)
        link = relates(first, "corrects")
        valid = (latest is not None and first.event_id == latest.event_id and len(cited_events) == 1
                 and claim.last_aligned_reported_status == event_condition(first, events)
                 and (not link or set(link.evidence_ids) <= main.keys())
                 and all(contains_subject(item.excerpt, claim.subject_id, aliases) for item in coverage.values()))
    if valid:
        return verdict("supported", "The independent check found matching reviewed events, timestamps, and source excerpts.")
    return verdict("contradicted", "The draft's structured claim disagrees with the cited published events or links.")


async def verify_claim(claim: Claim) -> tuple[Verdict, list[EvidenceResult]]:
    pin = {"exercise_id": claim.exercise_id, "revision_id": claim.revision_id}
    main, coverage = {}, {}
    try:
        async with Client(create_server(), read_timeout_seconds=10, cache=None) as client:
            try:
                for ids, destination in ((claim.evidence_ids, main), (claim.coverage_evidence_ids, coverage)):
                    for key in dict.fromkeys(ids):
                        body = await read_tool(client, "get_evidence", {**pin, "evidence_id": key})
                        item = EvidenceResult.model_validate(body)
                        if item.evidence_id != key or (item.exercise_id, item.revision_id) != (claim.exercise_id, claim.revision_id):
                            raise MissingEvidence("The citation did not resolve to the selected published revision.")
                        destination[key] = item
                events = await reviewed_events(client, pin, claim.as_of_ms) if claim.event_ids else {}
                uri = f"exercise-review://exercises/{claim.exercise_id}/revisions/{claim.revision_id}/glossary"
                resource = await client.read_resource(uri)
                glossary = json.loads(resource.contents[0].text)["glossary"]
                verdict = evaluate_claim(claim, main, coverage, events, aliases_for(glossary))
                return verdict, [*main.values(), *coverage.values()]
            except MissingEvidence as error:
                return Verdict(status="insufficient_evidence", reason=str(error)), [*main.values(), *coverage.values()]
    except (OSError, ExceptionGroup):
        return Verdict(status="insufficient_evidence", reason="The independent local MCP verifier could not complete its check."), [
            *main.values(), *coverage.values()
        ]
