"""Optional chart spelling suggestions for archived radio transcripts."""

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .radio_messages import NUMBER

FIX_SLOT = re.compile(
    rf"\b(?:direct(?:\s+to)?|(?:re)?cleared\s+(?:via|at)|cross|"
    rf"{NUMBER}\s+(?:knots?\s+)?(?:to|until|till|til))\s+(?P<fix>[a-z]{{4,6}})\b",
    re.I,
)
FROM_FIX = re.compile(r"\bfrom\s+(?P<fix>[a-z]{4,6})(?=,?\s+(?:intercept|enter|cleared)\b)", re.I)
REPEATED_FIX = re.compile(r"\b(?P<fix>[a-z]{4,6})(?=\s+cross\s+(?P=fix)\b)", re.I)
ORDINARY_WORDS = frozenset(
    {"ahead", "final", "flight", "ground", "left", "north", "radar", "right", "route", "runway",
     "south", "there", "tower"}
)


class FixVocabulary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    terms: Annotated[list[Annotated[str, StringConstraints(pattern=r"^[A-Z]{5}$")]],
                     Field(min_length=1, max_length=1024)]

    @field_validator("terms")
    @classmethod
    def unique_terms(cls, terms: list[str]) -> list[str]:
        if len(set(terms)) != len(terms):
            raise ValueError("Fix codes must be unique.")
        return terms


def one_letter_apart(first: str, second: str) -> bool:
    if len(first) == len(second):
        return sum(a != b for a, b in zip(first, second, strict=True)) == 1
    shorter, longer = sorted((first, second), key=len)
    return len(longer) - len(shorter) == 1 and any(
        longer[:i] + longer[i + 1:] == shorter for i in range(len(longer))
    )


def suggest_fix_spellings(text: str, vocabulary: FixVocabulary) -> tuple[str, list[dict[str, str]]]:
    edits = []
    exact = set(vocabulary.terms)

    def replace(match: re.Match) -> str:
        heard = match["fix"]
        if heard.casefold() in ORDINARY_WORDS or heard.upper() in exact:
            return match[0]
        choices = [term for term in vocabulary.terms if one_letter_apart(heard.upper(), term)]
        if len(choices) != 1:
            return match[0]
        edits.append({"original": heard, "suggested": choices[0]})
        return match[0][:(match.start("fix") - match.start())] + choices[0]

    # A literal repeated fix immediately before "cross [fix]" belongs to that same reference.
    for pattern in (REPEATED_FIX, FIX_SLOT, FROM_FIX):
        text = pattern.sub(replace, text)
    return text, edits
