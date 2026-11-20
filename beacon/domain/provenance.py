"""References to verbatim rule text."""

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class RuleSource(StrEnum):
    """A body of rules, matching a file in rules/."""

    JOINT = "JOINT"
    HOUSE = "HOUSE"
    SENATE = "SENATE"


@dataclass(frozen=True)
class ClauseRef:
    """A numbered rule, or one paragraph of it (1-based)."""

    source: RuleSource
    rule: str
    paragraph: int | None = None

    def __str__(self) -> str:
        base = f"{self.source.value.title()} Rule {self.rule}"
        return base if self.paragraph is None else f"{base} para {self.paragraph}"


@dataclass(frozen=True)
class Clause:
    """Verbatim text for a clause reference."""

    ref: ClauseRef
    text: str

    @property
    def text_hash(self) -> str:
        """sha256 of the clause text."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()
