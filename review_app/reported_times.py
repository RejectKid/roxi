import re

MAX_SECONDS = 999 * 3600 + 3599
NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
)
NUMBERS = {word: str(value) for value, word in enumerate(NUMBER_WORDS)}
NUMBERS["oh"] = "0"
TOKEN = r"(?:[0-9]+|(?:" + "|".join(NUMBERS) + r")\b)"
CLOCK = TOKEN + r"(?:[ \t,:+-]+(?:plus[ \t+-]+)?" + TOKEN + r")*"
NUMBER = re.compile(TOKEN, re.I)
SELF_REPAIR = re.compile(r"\b(?:my (?:error|mistake)|i (?:meant|misspoke))\b", re.I)
CORRECTION_PREFIX = re.compile(r"\b(?:correction|i meant)[:,]?\s*$", re.I)
RECEIPT_REVISION = re.compile(r"\b(?:roger|copy)\b[.,]?\s+that\s*$", re.I)


def format_reported_time(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def reported_time(body: str, aliases: list[str]) -> tuple[int, bool] | None:
    if not aliases:
        return None
    names = "|".join(map(re.escape, sorted(aliases, key=len, reverse=True)))
    pattern = rf"(?<![\w-])(?:{names})(?![\w-])\s+(?P<verb>should be|is|at)\s+(?P<clock>{CLOCK})(?![\w:])"
    matches = set()
    for result in re.finditer(pattern, body, re.I):
        prefix = body[:result.start()]
        correction = bool(CORRECTION_PREFIX.search(prefix) or SELF_REPAIR.search(body)
                          or result["verb"].casefold() == "should be" and RECEIPT_REVISION.search(prefix))
        if result["verb"].casefold() == "should be" and not correction:
            continue
        clock = result["clock"]
        if ":" in clock and not re.fullmatch(r"\d{1,3}:[0-5]\d:[0-5]\d", clock):
            continue
        tokens = NUMBER.findall(clock.casefold())
        digits = "".join(NUMBERS.get(token, token) for token in tokens)
        if not 5 <= len(digits) <= 7:
            continue
        hours, minutes, seconds = int(digits[:-4]), int(digits[-4:-2]), int(digits[-2:])
        if minutes < 60 and seconds < 60:
            matches.add((hours * 3600 + minutes * 60 + seconds, correction))
    return matches.pop() if len(matches) == 1 else None


def time_replaces(event: dict, target: dict) -> bool:
    revised = event.get("reported_time_seconds")
    old = target.get("reported_time_seconds")
    return old is not None and revised is not None and old != revised and target["subject_id"] == event["subject_id"]
