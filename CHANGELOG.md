# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Findings can be published to the Tracker website. Each committee's latest results are
  uploaded together and appear on the site at the same moment.
- These release notes are published on the Tracker website alongside the findings.
- Vote records and bill summaries that a committee posts on its Documents tab, such as
  poll results, now count as posted. Several committees publish their votes only there.
- Checks can now cover every joint committee at once, leaving out House and Senate
  committees.

### Changed

- Whether a summary or vote record was posted on time is now judged by the date
  malegislature.gov lists it as posted, when the site shows one, rather than the date
  the file was created. That is when the public could first see it. If the same
  document appeared earlier elsewhere on the site, the earlier date counts.
- Ruleset s194, version 6.

### Fixed

- Committee vote records attached to a hearing now count as posted votes. Previously
  they were mistaken for the bill's summary, which could show a summary as posted when
  it was not.

## [0.1.0] - 2026-11-19

### Added

- Checks each bill referred to a committee in the 194th General Court against four
  requirements: reporting by the deadline (Joint Rule 10, House Rule 27), announcing
  hearings in advance (Joint Rule 1D, Senate Rule 12, House Rule 17A), posting a
  plain-language summary (Joint Rule 1B), and posting committee votes (Joint Rule 1D,
  House Rule 17B).
- Ruleset s194, version 5.
- Every finding cites the verbatim rule text it was judged against and the evidence
  found on malegislature.gov.
- Findings judged against an older version of a rule's text are flagged.
- A per-committee audit report showing how each finding was derived.

[unreleased]: https://github.com/arbowl/beacon-hill-compliance-tracker-v2/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/arbowl/beacon-hill-compliance-tracker-v2/releases/tag/v0.1.0
