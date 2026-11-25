"""Parse explicit speed-limit corrections and matching radio readbacks for archive review."""

import re
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

RESPONSE_WINDOW_MS = 5_000
DIGITS = {word: number for number, word in enumerate(
    ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
)}
DIGITS.update({"oh": 0, "niner": 9})
TEENS = {word: number for number, word in enumerate(
    ("ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"), 10
)}
TENS = {word: number * 10 for number, word in enumerate(
    ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"), 2
)}
WORDS = {**DIGITS, **TEENS, **TENS, "hundred": 100}
NUMBER = r"(?:[1-9]\d{1,2}|(?:" + "|".join(WORDS) + r")(?:[ -]+(?:" + "|".join(WORDS) + r")){0,3})"
LIMIT = rf"(?P<speed>{NUMBER})\s+(?:knots?\s+)?(?:until|til|till|to)\s+(?P<fix>[a-z][a-z0-9]{{2,11}})\b"
ORIGINAL = re.compile(rf"\b(?:maintain(?: the)?|speed(?: of)?)\s+{LIMIT}[\s,.;—-]*$", re.I)
REPLACEMENT = re.compile(rf"^[\s,.:;—-]*(?:(?:maintain|speed)\s+)?{LIMIT}", re.I)
READBACK = re.compile(rf"^(?:(?:roger|copy|okay|affirmative)[,. ]+)?{LIMIT}", re.I)
REPAIR = re.compile(r"\b(?:correction|make that|i meant)\b", re.I)


class RadioSpeedLimit(BaseModel):
    knots: Annotated[int, Field(ge=10, le=999)]
    until: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9]{2,11}$")]


def speed_number(phrase: str) -> int | None:
    if phrase.isdecimal():
        return int(phrase)
    tokens = re.split(r"[ -]+", phrase.lower())
    if 2 <= len(tokens) <= 3 and all(token in DIGITS for token in tokens):
        return int("".join(str(DIGITS[token]) for token in tokens))
    hundreds = 0
    if len(tokens) > 1 and DIGITS.get(tokens[0], 0):
        if tokens[1] == "hundred" or tokens[1] in TEENS or tokens[1] in TENS:
            hundreds = DIGITS[tokens.pop(0)] * 100
            if tokens[0] == "hundred":
                tokens.pop(0)
    if not tokens:
        return hundreds
    if len(tokens) == 1 and tokens[0] != "hundred":
        return hundreds + WORDS[tokens[0]]
    if len(tokens) == 2 and tokens[0] in TENS and DIGITS.get(tokens[1], 0):
        return hundreds + TENS[tokens[0]] + DIGITS[tokens[1]]
    return None


def limit_value(match: re.Match | None) -> RadioSpeedLimit | None:
    if match:
        knots = speed_number(match["speed"].lower())
        if knots is not None and 10 <= knots <= 999:
            return RadioSpeedLimit(knots=knots, until=match["fix"].upper())
    return None


def inline_speed_correction(text: str) -> tuple[RadioSpeedLimit, RadioSpeedLimit] | None:
    markers = list(REPAIR.finditer(text))
    if len(markers) == 1:
        marker = markers[0]
        before = limit_value(ORIGINAL.search(text[:marker.start()]))
        after = limit_value(REPLACEMENT.match(text[marker.end():]))
        if before and after and before != after:
            return before, after
    return None


def speed_readback(text: str) -> RadioSpeedLimit | None:
    return limit_value(READBACK.match(text.strip()))


def format_limit(limit: RadioSpeedLimit) -> str:
    return f"{limit.knots} knots until {limit.until}"
