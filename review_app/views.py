from datetime import timezone

from fastapi.templating import Jinja2Templates

from .storage import ROOT

templates = Jinja2Templates(directory=ROOT / "review_app" / "templates")

EVENT_LABELS = {"status_report": "Report", "acknowledgment": "Acknowledgment", "correction": "Correction"}
STATUS_LABELS = {"in_service": "In service", "out_of_service": "Out of service", "unknown": "Unspecified"}
STATE_LABELS = {"proposed": "Proposed", "accepted": "Accepted", "rejected": "Rejected"}


def clock(milliseconds: int, precise: bool = False) -> str:
    minutes, remainder = divmod(milliseconds, 60000)
    seconds, fractional = divmod(remainder, 1000)
    result = f"{minutes:02d}:{seconds:02d}"
    return f"{result}.{fractional:03d}" if precise else result


templates.env.filters["clock"] = clock
templates.env.filters["date_utc"] = lambda value: value.astimezone(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")
templates.env.globals.update(event_labels=EVENT_LABELS, status_labels=STATUS_LABELS, state_labels=STATE_LABELS)
