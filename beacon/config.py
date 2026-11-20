"""Settings loaded from beacon.toml."""

import hashlib
import tomllib
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _check_site_url(url: str) -> str:
    """The site's base URL; plain http only to this machine, so keys are not exposed."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Site URL must look like https://host[:port]")
    if parts.query or parts.fragment:
        raise ValueError("Site URL must not have a query or fragment")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("Site URL must use https unless it is on this machine")
    return url.rstrip("/")


SiteUrl = Annotated[str, AfterValidator(_check_site_url)]


class FetchSettings(BaseModel):
    """How politely and how concurrently to fetch."""

    model_config = ConfigDict(frozen=True)

    min_interval: float = 0.5
    max_workers: int = 4
    timeout: float = 30.0
    retries: int = 1


class ScheduleSettings(BaseModel):
    """When document finders re-run for a bill whose page is unchanged."""

    model_config = ConfigDict(frozen=True)

    sweep_days: int = 7


class SiteSettings(BaseModel):
    """Where `beacon push` sends bundles and the changelog; key from BEACON_SITE_KEY."""

    model_config = ConfigDict(frozen=True)

    url: SiteUrl | None = None
    changelog: Path = Path("CHANGELOG.md")


class Settings(BaseModel):
    """Top-level configuration."""

    model_config = ConfigDict(frozen=True)

    data_dir: Path = Path("data")
    base_url: str = "https://malegislature.gov"
    session: int = 194
    committees: tuple[str, ...] = ("J19",)
    ruleset: str = "s194"
    rules_dir: Path = Path("rules")
    fetch: FetchSettings = FetchSettings()
    schedule: ScheduleSettings = ScheduleSettings()
    site: SiteSettings = SiteSettings()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "beacon.db"

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"

    def fingerprint(self) -> str:
        """sha256 of the settings, recorded on every run."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def load_settings(path: Path) -> Settings:
    """Read settings from a TOML file; missing file means defaults."""
    if not path.exists():
        return Settings()
    return Settings.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
