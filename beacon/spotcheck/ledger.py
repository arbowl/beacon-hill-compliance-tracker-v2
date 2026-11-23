"""The append-only ledger of audited tenures."""

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from beacon.spotcheck.records import LedgerEntry, Manifest, Verdict, VerdictSheet


class LedgerError(ValueError):
    """A verdict sheet does not fit its manifest or the ledger."""


def read(path: Path) -> list[LedgerEntry]:
    """Every entry, oldest first; a missing ledger is empty."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [LedgerEntry.model_validate_json(line) for line in lines if line.strip()]


def audited(entries: Iterable[LedgerEntry]) -> frozenset[tuple[str, str]]:
    """(tenure_id, input_fingerprint) pairs already audited."""
    return frozenset((e.tenure_id, e.input_fingerprint) for e in entries)


def entries_for(
    manifest: Manifest,
    sheet: VerdictSheet,
    existing: Sequence[LedgerEntry],
    audited_at: datetime,
) -> list[LedgerEntry]:
    """Ledger entries for a sheet's verdicts, checked against the manifest.

    Raises LedgerError for an unknown item, a requirement the item was not
    evaluated against, or an item this sample already recorded.
    """
    recorded = {(e.sample_id, e.tenure_id) for e in existing}
    entries = []
    for verdict in sheet.items:
        try:
            item = manifest.get(verdict.item)
        except KeyError:
            raise LedgerError(
                f"item {verdict.item} is not in {manifest.sample_id}"
            ) from None
        if (manifest.sample_id, item.tenure_id) in recorded:
            raise LedgerError(
                f"item {verdict.item} of {manifest.sample_id} is already recorded"
            )
        requirements = {r.requirement_id for r in item.results}
        for finding in verdict.findings:
            if (
                finding.requirement_id is not None
                and finding.requirement_id not in requirements
            ):
                raise LedgerError(
                    f"item {verdict.item} was not evaluated against "
                    f"{finding.requirement_id}; expected one of "
                    f"{', '.join(sorted(requirements))}"
                )
        entries.append(
            LedgerEntry(
                audited_at=audited_at,
                auditor=sheet.auditor,
                sample_id=manifest.sample_id,
                seed=manifest.seed,
                session=item.session,
                bill_id=item.bill_id,
                committee_id=item.committee_id,
                tenure_id=item.tenure_id,
                ruleset_id=manifest.ruleset_id,
                ruleset_version=manifest.ruleset_version,
                code_version=manifest.code_version,
                input_fingerprint=item.input_fingerprint,
                as_of=manifest.as_of,
                signature=item.signature,
                label=item.label,
                verdict=verdict.verdict,
                expected_label=verdict.expected_label,
                findings=verdict.findings,
                note=verdict.note,
            )
        )
    return entries


def append(path: Path, entries: Sequence[LedgerEntry]) -> None:
    """Append entries, one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(entry.model_dump_json() + "\n")


def _counts(title: str, counter: Counter[str]) -> list[str]:
    lines = [title]
    lines += [f"  {key}: {count}" for key, count in counter.most_common()]
    return lines


def summary(entries: Sequence[LedgerEntry]) -> str:
    """Verdicts per ruleset version, and findings by category and requirement."""
    if not entries:
        return "The ledger is empty."
    lines = [f"{len(entries)} audited tenures"]
    versions = sorted({(e.ruleset_id, e.ruleset_version) for e in entries})
    for ruleset_id, version in versions:
        scoped = [
            e
            for e in entries
            if (e.ruleset_id, e.ruleset_version) == (ruleset_id, version)
        ]
        verdicts = Counter(e.verdict.value for e in scoped)
        agree = verdicts[Verdict.AGREE.value]
        lines.append(
            f"{ruleset_id} v{version}: {agree}/{len(scoped)} agree; "
            + ", ".join(f"{v} {n}" for v, n in sorted(verdicts.items()))
        )
    findings = [f for e in entries for f in e.findings]
    if findings:
        lines += _counts(
            "Findings by category:", Counter(f.category.value for f in findings)
        )
        lines += _counts(
            "Findings by requirement:",
            Counter(f.requirement_id or "(none)" for f in findings),
        )
    return "\n".join(lines)
