"""CHANGELOG.md parses into the contract's Changelog and is pushed to the site."""

from datetime import date
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from beacon_contract import v1

from beacon.adapters.changelog import ChangelogError, parse
from beacon.adapters.site_push import CHANGELOG_PATH, SitePusher

REPO_CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"

SAMPLE = """# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- Not sent.

## [0.2.0] - 2026-12-01

### Changed

- Hearing notice counts calendar days,
  not business days.

### Fixed

- Hearings moved to a new date are read.

## [0.1.0] - 2026-11-19

### Added

- Checks four requirements.

[unreleased]: https://example.org/compare/v0.2.0...HEAD
[0.2.0]: https://example.org/compare/v0.1.0...v0.2.0
"""


def test_repo_changelog_matches_package_version():
    changelog = parse(REPO_CHANGELOG.read_text(encoding="utf-8"), version("beacon"))
    assert changelog.releases[0].version == version("beacon")


def test_parse_sample():
    changelog = parse(SAMPLE, "0.2.0")
    assert [r.version for r in changelog.releases] == ["0.2.0", "0.1.0"]
    newest = changelog.releases[0]
    assert newest.released_on == date(2026, 12, 1)
    assert [s.kind for s in newest.sections] == [
        v1.ChangeKind.CHANGED,
        v1.ChangeKind.FIXED,
    ]
    assert newest.sections[0].items == (
        "Hearing notice counts calendar days, not business days.",
    )


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (SAMPLE, "not the newest release"),
        (SAMPLE.replace("## [0.2.0] - 2026-12-01", "## 0.2.0"), "release heading"),
        (SAMPLE.replace("### Fixed", "### Improved"), "kind"),
        (SAMPLE.replace("- Hearings moved", "Hearings moved"), "expected a '- ' item"),
        (SAMPLE.replace("### Changed\n\n", ""), "before a ### section"),
    ],
    ids=["version mismatch", "heading", "unknown kind", "bare paragraph", "no section"],
)
def test_parse_rejects(text: str, match: str):
    with pytest.raises(ChangelogError, match=match):
        parse(text, "0.1.0" if match == "not the newest release" else "0.2.0")


def test_push_changelog():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = v1.Changelog.model_validate_json(request.content)
        state = v1.ChangelogState(
            releases=tuple(r.version for r in body.releases), updated=("0.2.0",)
        )
        return httpx.Response(200, content=state.model_dump_json())

    client = httpx.Client(
        base_url="https://site.test", transport=httpx.MockTransport(handler)
    )
    state = SitePusher(client).push_changelog(parse(SAMPLE, "0.2.0"))
    assert state.updated == ("0.2.0",)
    assert [(r.method, r.url.path) for r in seen] == [("PUT", CHANGELOG_PATH)]
