"""Loads numbered rules from rules/*.md into addressable clauses."""

import re
from dataclasses import dataclass
from pathlib import Path

from beacon.domain.provenance import Clause, ClauseRef, RuleSource

_HALF = chr(0xBD)
_RULE_START = re.compile(r"^\s*(\d+)([A-Z]*(?:\s*" + _HALF + r")?)\s*\.\s+(.*)$")
_NOTE_ONLY = re.compile(r"^\[[^\]]*\]$")
_HEADING_MAX_LEN = 80


def _sort_key(number: int, suffix: str) -> tuple[int, str]:
    return number, suffix.replace(" ", "").replace(_HALF, "-1/2")


def _is_heading(paragraph: str) -> bool:
    if len(paragraph) > _HEADING_MAX_LEN or "shall" in paragraph.lower():
        return False
    words = [w for w in paragraph.split() if w[0].isalpha()]
    capitalized = sum(1 for w in words if w[0].isupper())
    return paragraph.isupper() or (bool(words) and capitalized / len(words) >= 0.5)


def _blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def parse_rules(text: str) -> dict[str, tuple[str, ...]]:
    """Split rule text into {rule number: paragraphs}, in document order.

    A rule starts at a paragraph beginning "N." or "NA." whose number is greater
    than the previous rule's. Paragraphs consisting only of a bracketed note are
    dropped, as is a short trailing heading before the next rule.
    """
    rules: dict[str, list[str]] = {}
    current: str | None = None
    last_key: tuple[int, str] = (0, "")
    for block in _blocks(text):
        paragraph = " ".join(line.strip() for line in block)
        match = _RULE_START.match(block[0])
        if match:
            key = _sort_key(int(match.group(1)), match.group(2))
            if key > last_key:
                current = f"{match.group(1)}{key[1]}"
                last_key = key
                body = " ".join(
                    [match.group(3).strip()] + [s.strip() for s in block[1:]]
                )
                rules[current] = [body]
                continue
        if current is None or _NOTE_ONLY.match(paragraph):
            continue
        rules[current].append(paragraph)
    for paragraphs in rules.values():
        if len(paragraphs) > 1 and _is_heading(paragraphs[-1]):
            paragraphs.pop()
    return {rule: tuple(paragraphs) for rule, paragraphs in rules.items()}


@dataclass(frozen=True)
class ClauseIndex:
    """All rules from every source, addressable by ClauseRef."""

    rules: dict[RuleSource, dict[str, tuple[str, ...]]]

    @staticmethod
    def load(rules_dir: Path) -> "ClauseIndex":
        """Parse every rules/<SOURCE>.md file present."""
        rules = {
            source: parse_rules(path.read_text(encoding="utf-8"))
            for source in RuleSource
            if (path := rules_dir / f"{source.value}.md").exists()
        }
        return ClauseIndex(rules)

    def paragraphs(self, source: RuleSource, rule: str) -> tuple[str, ...]:
        """All paragraphs of a rule."""
        return self.rules[source][rule]

    def get(self, ref: ClauseRef) -> Clause:
        """Resolve a reference to its verbatim text. Raises KeyError if absent."""
        paragraphs = self.paragraphs(ref.source, ref.rule)
        if ref.paragraph is None:
            return Clause(ref, "\n\n".join(paragraphs))
        if not 1 <= ref.paragraph <= len(paragraphs):
            raise KeyError(str(ref))
        return Clause(ref, paragraphs[ref.paragraph - 1])
