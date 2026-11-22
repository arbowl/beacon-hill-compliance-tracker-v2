# Beacon Hill Compliance Tracker

Checks whether Massachusetts Legislature committees follow their own rules, bill by
bill: reporting by the deadline (JR 10, HR 27), announcing hearings in advance
(JR 1D, SR 12, HR 17A), posting a plain-language summary (JR 1B), and posting
committee votes (JR 1D, HR 17B). Every verdict cites the rule text it was judged
against. Data comes from the public site, malegislature.gov.

Each stage is independent and talks only to a local store, so rules can be re-run
over old data and parser fixes can be applied to pages already captured.

## Pipeline

```
 malegislature.gov
        |
        v
  +-----------+   committees, bill lists, hearings (dockets, documents)
  |  collect  |----------------------------------------------+
  +-----------+                                              |
        |                                                    v
  +-----------+   bill pages, vote/summary tabs, PDFs   +---------+
  |  scrape   |---------------------------------------->|  store  |
  +-----------+   (captures + observations)             | sqlite  |
                                                        | + blobs |
  +-----------+   re-derive observations from stored    |         |
  | reextract |<--------------------------------------->|         |
  +-----------+   captures when an extractor changes    |         |
                                                        |         |
  +-----------+   facts -> ruleset (s194) -> verdicts   |         |
  | evaluate  |<--------------------------------------->|         |
  +-----------+   with derivations and rule citations   |         |
                                                        |         |
  +-----------+   JSON export, HTML audit report        |         |
  |  report   |<----------------------------------------|         |
  +-----------+                                         +---------+
        |
        v
  data/reports/<date>/<committee>.json, <committee>_audit.html
```

`run mvp` = collect, scrape, reextract, evaluate, report (committee by committee;
reextract once, first). `run offline` = reextract, evaluate, report; no network.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```
uv sync
```

`beacon.toml` (all keys optional):

```toml
data_dir = "data"          # store, blobs, reports, logs, events
session = 194
committees = ["J19"]       # default scope; "all" = every listed committee; "joint", "house", "senate" = that chamber only
ruleset = "s194"
rules_dir = "rules"        # verbatim JOINT.md, HOUSE.md, SENATE.md

[fetch]
min_interval = 0.5         # seconds between requests to the site
max_workers = 4

[schedule]
sweep_days = 7             # re-search documents for unchanged bills this often

[site]
url = "http://127.0.0.1:5000"         # where `push` sends; key in BEACON_SITE_KEY
changelog = "CHANGELOG.md"             # sent before every push
```

## Running

Global options go before the command.

```
uv run beacon run mvp                         # default committees, full pipeline
uv run beacon --committee J45 run mvp         # one committee (repeatable)
uv run beacon --committee all run mvp         # every committee (long)
uv run beacon --committee joint run mvp       # every joint committee
uv run beacon --committee house run mvp       # every House committee (or senate)
uv run beacon run offline                     # re-check stored data, no network
uv run beacon --as-of 2026-03-18 evaluate     # as things stood on a date
uv run beacon --full scrape                   # search every bill's documents again
uv run beacon report                          # rewrite reports from the latest evaluation
uv run beacon serve                           # control center at http://127.0.0.1:8765
```

Other flags: `--quiet` (no console progress), `--json-events` (events to stdout),
`--v1 <basic_X.json>` (compare against an output file from the original tracker), `--config <path>`.

Only one run can use a data directory at a time; a second exits with an error.

### Control center

`beacon serve` also has a schedule and a way to send results to the site, both saved
to `data/control.json`.

- **Schedule**: a plan, committees, a time, and weekdays, in State House (Eastern)
  time. It runs only while `serve` is running. A time missed while it was closed is
  skipped; one missed while the machine slept runs once on waking. If another run
  holds the data directory, that check is skipped and the reason is shown. To run
  without the control center, schedule `uv run beacon run mvp` with the operating
  system's scheduler instead.
- **Send to the website**: a site address and ingest key. Saved values take
  precedence over `[site] url` and `BEACON_SITE_KEY`. **Send now** pushes the newest
  bundle of every committee. **Send results after every check** pushes what each run
  wrote, including scheduled runs. Runs with an as-of date are never sent. The key is
  stored in plain text in `control.json` and is never shown in the page. Changing the
  address clears the saved key. Plain `http` is accepted only for this machine.

The control center answers only requests addressed to `127.0.0.1` or `localhost`
(or the `--host` given), and refuses changes sent from other web pages.

## Output

```
data/
  beacon.db                      runs, captures, inventory, observations, evaluations
  blobs/                         every fetched page, gzip, by sha256
  reports/<date>/                <committee>.json, <committee>_audit.html
  events/                        <run_id>.jsonl, plan-<id>.jsonl (live progress)
  logs/beacon.log
```

## Versioning and the changelog

[CHANGELOG.md](CHANGELOG.md) follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the version follows [SemVer](https://semver.org/spec/v2.0.0.html). The site shows the
changelog to the public at `/changes`, so write entries in plain language, in the Tracker's
voice (detections, not judgments), and keep developer detail in commit messages.

| Bump | When |
|---|---|
| Major | A breaking change to the push contract (a new `beacon_contract` schema version) |
| Minor | Anything that can change a finding: a ruleset `version` bump, an `EXTRACTOR_VERSION` or `FINDER_VERSION` bump, a committee name change, new rule text |
| Patch | A fix that changes no finding (logging, speed, the GUI, the local HTML report) |

A minor or major release also puts a short notice on the site's dashboard for 14 days.

To release:

1. Note each change under `## [Unreleased]` as you make it.
2. Rename that heading to `## [X.Y.Z] - YYYY-MM-DD`, add a fresh `## [Unreleased]` above
   it, and update the links at the bottom.
3. Set `version` in `pyproject.toml` to the same `X.Y.Z`, run `uv sync`, and tag `vX.Y.Z`.

`beacon push` (and `run mvp`) sends every released version before the bundles. It
refuses to push if the newest release does not match the installed version, if the file
strays from the format (headings `## [X.Y.Z] - YYYY-MM-DD` and `### Added`, `Changed`,
`Deprecated`, `Removed`, `Fixed`, or `Security`, then `- ` items), or if a release the site
already shows is missing. The test suite checks the first two. A release's text can
be corrected later, and the site keeps the earlier text.

## Making changes

| To change | Edit | Then |
|---|---|---|
| A rule's interpretation | `beacon/evaluator/rulesets/s194/` (bump `version` in `ruleset.py`) | `run offline` |
| How history rows are read | `beacon/scraper/history/` (bump `EXTRACTOR_VERSION`) | `run offline` |
| How documents are found | `beacon/scraper/documents/` (bump `FINDER_VERSION`) | `run offline`; new pages need `run mvp` |
| Committee names | `beacon/scraper/history/committees.py` (retired names: `active_until`) | bump `EXTRACTOR_VERSION` and `FINDER_VERSION`, `run offline` |
| Rule text | `rules/*.md` (reports flag verdicts judged against older text) | `run offline` |
| Page noise ignored when detecting changes | `beacon/adapters/site.py` | none |

Checks:

```
uv run pytest
uv run ruff format . && uv run ruff check .
uv run ty check
```
