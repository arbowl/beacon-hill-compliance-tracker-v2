"""Seeded sampling of stored evaluations, spread evenly across outcome signatures."""

import random
from collections.abc import Iterable, Iterator, Sequence

from beacon.domain.evaluation import ComplianceLabel, StepRole, TenureEvaluation
from beacon.ports.store import Store

_NOTABLE_ROLES = (StepRole.EXTENSION, StepRole.EXTENSION_IGNORED, StepRole.CAP)


def signature(evaluation: TenureEvaluation) -> str:
    """The label, each requirement's status and reason, and any notable steps.

    Evaluations sharing a signature took the same path through the ruleset.
    """
    parts = []
    for result in sorted(evaluation.results, key=lambda r: r.requirement_id):
        part = f"{result.requirement_id}={result.status.value}"
        if result.reason is not None:
            part += f":{result.reason.value}"
        roles = sorted(
            {s.role.value for s in result.derivation if s.role in _NOTABLE_ROLES}
        )
        if roles:
            part += f"[{'+'.join(roles)}]"
        parts.append(part)
    return f"{evaluation.label.value} | {', '.join(parts)}"


def frame(
    store: Store,
    session: int,
    ruleset_id: str,
    committees: Sequence[str],
    label: ComplianceLabel | None = None,
) -> list[TenureEvaluation]:
    """Evaluations from each committee's latest successful run."""
    evaluations: list[TenureEvaluation] = []
    for committee_id in committees:
        run_id = store.evaluations.latest_run(session, committee_id, ruleset_id)
        if run_id is None:
            continue
        evaluations.extend(
            e
            for e in store.evaluations.for_run(run_id)
            if e.committee_id == committee_id and (label is None or e.label == label)
        )
    return evaluations


def _interleave[T](streams: list[Iterator[T]]) -> Iterator[T]:
    """One item from each stream in turn until all are exhausted."""
    while streams:
        active = []
        for stream in streams:
            for item in stream:
                yield item
                active.append(stream)
                break
        streams = active


def draw(
    evaluations: Iterable[TenureEvaluation], seed: int
) -> Iterator[TenureEvaluation]:
    """Every evaluation once, rotating across labels, then signatures within each.

    Rare paths through the ruleset surface as early as common ones, and no label
    crowds out the others. The order depends only on the seed and the evaluations.
    """
    rng = random.Random(seed)
    groups: dict[ComplianceLabel, dict[str, list[TenureEvaluation]]] = {}
    for e in sorted(evaluations, key=lambda e: (e.tenure_id, str(e.evaluation_id))):
        groups.setdefault(e.label, {}).setdefault(signature(e), []).append(e)
    labels = []
    for label in sorted(groups):
        queues = [groups[label][key] for key in sorted(groups[label])]
        rng.shuffle(queues)
        for queue in queues:
            rng.shuffle(queue)
        labels.append(_interleave([iter(q) for q in queues]))
    rng.shuffle(labels)
    return _interleave(labels)
