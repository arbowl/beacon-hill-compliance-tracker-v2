"""Reads CHANGELOG.md (Keep a Changelog) into the contract's Changelog."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from beacon_contract import v1
from pydantic import ValidationError

_RELEASE = re.compile(r"^## \[(?P<version>[^\]]+)\] - (?P<date>\d{4}-\d{2}-\d{2})$")
_UNRELEASED = "## [Unreleased]"
_SECTION = re.compile(r"^### (?P<kind>.+)$")
_ITEM = re.compile(r"^- (?P<text>.+)$")
_CONTINUATION = re.compile(r"^ {2,}(?P<text>\S.*)$")
_LINK_REFERENCE = re.compile(r"^\[[^\]]+\]: \S+$")


class ChangelogError(ValueError):
    """CHANGELOG.md is not in the supported Keep a Changelog form."""


@dataclass
class _Draft:
    version: str
    released_on: str
    sections: list[tuple[str, list[str]]] = field(default_factory=list)


def parse(text: str, code_version: str) -> v1.Changelog:
    """Every released version, in file order. Skips Unreleased, preamble, and links.

    Items are plain text; continuation lines of a wrapped item join with one space.
    """
    releases: list[_Draft] = []
    current: _Draft | None = None
    skipping = True
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if line.startswith("## "):
            match = _RELEASE.match(line)
            if line == _UNRELEASED:
                current, skipping = None, True
            elif match:
                current, skipping = _Draft(match["version"], match["date"]), False
                releases.append(current)
            else:
                raise ChangelogError(
                    f"line {number}: unrecognized release heading {line!r}"
                )
        elif skipping or not line or _LINK_REFERENCE.match(line):
            continue
        elif current is None:
            raise ChangelogError(f"line {number}: text outside a release")
        elif match := _SECTION.match(line):
            current.sections.append((match["kind"], []))
        elif not current.sections:
            raise ChangelogError(f"line {number}: text before a ### section heading")
        elif match := _ITEM.match(line):
            current.sections[-1][1].append(match["text"])
        elif (match := _CONTINUATION.match(line)) and current.sections[-1][1]:
            items = current.sections[-1][1]
            items[-1] = f"{items[-1]} {match['text']}"
        else:
            raise ChangelogError(f"line {number}: expected a '- ' item, got {line!r}")
    try:
        return v1.Changelog.model_validate(
            {
                "schema_version": v1.SCHEMA_VERSION,
                "code_version": code_version,
                "releases": [
                    {
                        "version": r.version,
                        "released_on": r.released_on,
                        "sections": [{"kind": k, "items": i} for k, i in r.sections],
                    }
                    for r in releases
                ],
            }
        )
    except ValidationError as exc:
        raise ChangelogError(str(exc)) from exc


def load(path: Path, code_version: str) -> v1.Changelog:
    if not path.exists():
        raise ChangelogError(f"No changelog at {path}")
    return parse(path.read_text(encoding="utf-8"), code_version)
