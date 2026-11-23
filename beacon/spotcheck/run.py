"""Draws a spot-check sample to disk and records verdicts on it."""

from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from beacon.domain.evaluation import ComplianceLabel
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.rulesets.base import Ruleset
from beacon.ports.blobs import BlobStore
from beacon.ports.clock import Clock
from beacon.ports.store import Store
from beacon.spotcheck import ledger
from beacon.spotcheck.dossier import render_dossier, render_key, render_rules
from beacon.spotcheck.records import (
    LedgerEntry,
    Manifest,
    ManifestItem,
    ResultSummary,
    VerdictSheet,
)
from beacon.spotcheck.replay import Case, Replayer
from beacon.spotcheck.sampling import draw, frame, signature

MANIFEST = "manifest.json"
VERDICTS = "verdicts.json"
RULES = "rules.md"


class SpotcheckError(RuntimeError):
    """A spot check could not be drawn or recorded."""


@dataclass(frozen=True)
class Sources:
    """What a spot check reads from."""

    store: Store
    blobs: BlobStore
    clauses: ClauseIndex
    ruleset: Ruleset
    clock: Clock
    session: int
    code_version: str


@dataclass(frozen=True)
class SampleRequest:
    """How many tenures to draw, from where, and with which seed."""

    count: int
    seed: int
    committees: tuple[str, ...] = ()
    label: ComplianceLabel | None = None


def _item(number: int, case: Case) -> ManifestItem:
    e, stem = (
        case.evaluation,
        f"{number:02d}-{case.stored.bill.bill_id}-{case.stored.committee_id}",
    )
    return ManifestItem(
        item=number,
        session=e.bill.session,
        bill_id=e.bill.bill_id,
        committee_id=e.committee_id,
        tenure_id=e.tenure_id,
        signature=signature(e),
        label=e.label,
        results=tuple(
            ResultSummary(
                requirement_id=r.requirement_id,
                status=r.status,
                reason=r.reason.value if r.reason else None,
            )
            for r in e.results
        ),
        input_fingerprint=e.input_fingerprint,
        stored_evaluation_id=case.stored.evaluation_id,
        stored_ruleset_version=case.stored.ruleset_version,
        stored_label=case.stored.label,
        dossier=f"{stem}.md",
        key=f"{stem}-key.md",
    )


def sample(
    sources: Sources, request: SampleRequest, out_root: Path, ledger_path: Path
) -> Path:
    """Write dossiers, keys, rule text, and a manifest; return the sample directory.

    Tenures are replayed with current code at the current moment. Ones already in
    the ledger with the same input fingerprint, and ones current code no longer
    evaluates, are skipped.
    """
    if request.count < 1:
        raise SpotcheckError("count must be at least 1")
    now = sources.clock.now()
    sample_id = f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-s{request.seed}"
    ruleset = sources.ruleset
    committees = request.committees or tuple(
        c.committee_id for c in sources.store.inventory.committees(sources.session)
    )
    evaluations = frame(
        sources.store, sources.session, ruleset.ruleset_id, committees, request.label
    )
    done = ledger.audited(ledger.read(ledger_path))
    replayer = Replayer(
        sources.store,
        sources.blobs,
        sources.clauses,
        ruleset,
        now,
        uuid5(NAMESPACE_URL, f"spotcheck:{sample_id}"),
    )
    cases: list[Case] = []
    for stored in draw(evaluations, request.seed):
        case = replayer.replay(stored)
        if (
            case is None
            or (case.evaluation.tenure_id, case.evaluation.input_fingerprint) in done
        ):
            continue
        cases.append(case)
        if len(cases) == request.count:
            break
    if not cases:
        raise SpotcheckError("no unaudited tenures match the request")
    directory = out_root / sample_id
    try:
        directory.mkdir(parents=True)
    except FileExistsError:
        raise SpotcheckError(f"sample {sample_id} already exists") from None
    items = []
    for number, case in enumerate(cases, 1):
        item = _item(number, case)
        (directory / item.dossier).write_text(
            render_dossier(number, case), encoding="utf-8"
        )
        (directory / item.key).write_text(render_key(number, case), encoding="utf-8")
        items.append(item)
    cited = {
        step.clause
        for case in cases
        for result in case.evaluation.results
        for step in result.derivation
        if step.clause is not None
    }
    (directory / RULES).write_text(
        render_rules(ruleset, sources.clauses, cited), encoding="utf-8"
    )
    manifest = Manifest(
        sample_id=sample_id,
        seed=request.seed,
        as_of=now,
        code_version=sources.code_version,
        ruleset_id=ruleset.ruleset_id,
        ruleset_version=ruleset.version,
        committees=committees,
        label_filter=request.label,
        frame_size=len(evaluations),
        items=tuple(items),
    )
    (directory / MANIFEST).write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    return directory


def record(directory: Path, ledger_path: Path, clock: Clock) -> list[LedgerEntry]:
    """Append the sample's verdicts.json to the ledger; return what was appended."""
    for name in (MANIFEST, VERDICTS):
        if not (directory / name).exists():
            raise SpotcheckError(f"{directory / name} does not exist")
    manifest = Manifest.model_validate_json(
        (directory / MANIFEST).read_text(encoding="utf-8")
    )
    sheet = VerdictSheet.model_validate_json(
        (directory / VERDICTS).read_text(encoding="utf-8")
    )
    entries = ledger.entries_for(manifest, sheet, ledger.read(ledger_path), clock.now())
    ledger.append(ledger_path, entries)
    return entries
