"""Uploads committee bundles to the Tracker site's ingest API."""

import gzip
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid5

import httpx
from beacon_contract import v1
from pydantic import BaseModel, ValidationError

from beacon.reporters.site_bundle import BUNDLE_SUFFIX

INGEST_PATH = "/api/ingest/v1/runs"
CHANGELOG_PATH = "/api/ingest/v1/changelog"
_PUSH_NAMESPACE = UUID("3b0f3c1e-8f0a-4a52-9c7e-1d2b6a4e5f70")


class PushError(RuntimeError):
    """The site refused a push, or replied with something unexpected."""


def latest_bundles(
    report_dir: Path, committees: Sequence[str] | None = None
) -> dict[str, Path]:
    """The newest bundle file per committee; every committee found if none are named."""
    found: dict[str, Path] = {}
    for path in sorted(
        report_dir.glob(f"*/*{BUNDLE_SUFFIX}"), key=lambda p: p.parent.name
    ):
        found[path.name.removesuffix(BUNDLE_SUFFIX)] = path
    if committees is None:
        return dict(sorted(found.items()))
    missing = [c for c in committees if c not in found]
    if missing:
        raise PushError(f"No bundle for {', '.join(missing)}; run report first")
    return {c: found[c] for c in committees}


def bundle_committees(paths: Iterable[Path]) -> list[str]:
    """Committee ids of the bundle files among paths."""
    return [
        p.name.removesuffix(BUNDLE_SUFFIX)
        for p in paths
        if p.name.endswith(BUNDLE_SUFFIX)
    ]


def load_bundle(path: Path) -> v1.CommitteeBundle:
    """Read and validate one bundle file."""
    return v1.CommitteeBundle.model_validate_json(gzip.decompress(path.read_bytes()))


def push_id(bundles: Sequence[v1.CommitteeBundle]) -> UUID:
    """Stable per set of evaluation runs, so pushing the same data twice is one push."""
    parts = sorted(f"{b.committee.committee_id}:{b.evaluation_run_id}" for b in bundles)
    return uuid5(_PUSH_NAMESPACE, "|".join(parts))


def manifest(
    bundles: Sequence[v1.CommitteeBundle], code_version: str, created_at: datetime
) -> v1.RunManifest:
    """The manifest that opens a push of these bundles."""
    sessions = {b.session for b in bundles}
    if len(sessions) != 1:
        raise PushError(f"Bundles span sessions {sorted(sessions)}")
    return v1.RunManifest(
        schema_version=v1.SCHEMA_VERSION,
        push_id=push_id(bundles),
        session=sessions.pop(),
        code_version=code_version,
        created_at=created_at,
        committees=tuple(b.committee.committee_id for b in bundles),
    )


def _reply[T: BaseModel](response: httpx.Response, model: type[T]) -> T:
    try:
        if response.is_success:
            return model.model_validate_json(response.content)
        error = v1.ErrorBody.model_validate_json(response.content).error
    except ValidationError as exc:
        raise PushError(
            f"HTTP {response.status_code}: unexpected reply ({exc})"
        ) from exc
    details = "; ".join(f"{'.'.join(map(str, d.loc))}: {d.msg}" for d in error.details)
    raise PushError(
        f"HTTP {response.status_code} {error.code}: {error.message} {details}".strip()
    )


def _state(response: httpx.Response) -> v1.RunState:
    return _reply(response, v1.RunState)


class SitePusher:
    """Opens a push, uploads each committee, then publishes; safe to re-run."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def push_changelog(self, changelog: v1.Changelog) -> v1.ChangelogState:
        """Send every released version; the site stores only what changed."""
        return _reply(
            self._client.put(
                CHANGELOG_PATH,
                content=changelog.model_dump_json(),
                headers={"Content-Type": "application/json"},
            ),
            v1.ChangelogState,
        )

    def push(
        self, manifest: v1.RunManifest, bundles: Mapping[str, bytes]
    ) -> v1.RunState:
        """Upload gzipped bundle JSON keyed by committee id, then publish."""
        state = _state(
            self._client.post(
                INGEST_PATH,
                content=manifest.model_dump_json(),
                headers={"Content-Type": "application/json"},
            )
        )
        if state.status == v1.RunStatus.PUBLISHED:
            return state
        if state.status != v1.RunStatus.OPEN:
            raise PushError(f"Push {state.push_id} is {state.status}")
        base = f"{INGEST_PATH}/{manifest.push_id}"
        for committee_id, body in bundles.items():
            state = _state(
                self._client.put(
                    f"{base}/committees/{committee_id}",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Encoding": "gzip",
                    },
                )
            )
        return _state(self._client.post(f"{base}/publish"))


def site_client(
    url: str,
    key: str,
    timeout: float = 120.0,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """An HTTP client for the site with the ingest key attached."""
    return httpx.Client(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout,
        transport=transport or httpx.HTTPTransport(retries=3),
    )
