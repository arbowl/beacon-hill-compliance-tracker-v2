"""What the control center keeps between sessions: the schedule and the push target."""

import json
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import IntEnum, StrEnum
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, model_validator

from beacon.adapters.site_push import PushError, site_client
from beacon.config import Settings, SiteUrl
from beacon.domain.local_time import LEGISLATURE_TZ
from beacon.orchestrator import runner
from beacon.orchestrator.runner import PLANS, PushOutcome, RunRequest

PREFERENCES_FILE = "control.json"


class Weekday(IntEnum):
    """Days as numbered by date.weekday()."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4
    SATURDAY = 5
    SUNDAY = 6


class Schedule(BaseModel):
    """A plan to run on chosen weekdays at a wall-clock time at the State House."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    at: time = time(1, 0)
    days: frozenset[Weekday] = frozenset(Weekday)
    plan: str = "mvp"
    committees: tuple[str, ...] = ()
    full: bool = False

    @model_validator(mode="after")
    def _valid(self) -> "Schedule":
        if not self.days:
            raise ValueError("Choose at least one day")
        if self.plan not in PLANS:
            raise ValueError(f"Unknown plan {self.plan!r}")
        if self.at.tzinfo is not None:
            raise ValueError("Give the time without a zone; it is State House time")
        return self

    def request(self) -> RunRequest:
        """The run this schedule starts; empty committees means the configured ones."""
        return RunRequest(plan=self.plan, committees=self.committees, full=self.full)

    def next_after(self, moment: datetime) -> datetime | None:
        """The first scheduled time strictly after moment, or None if disabled."""
        if not self.enabled:
            return None
        today = moment.astimezone(LEGISLATURE_TZ).date()
        for offset in range(8):
            day = today + timedelta(days=offset)
            candidate = datetime.combine(day, self.at, LEGISLATURE_TZ)
            if Weekday(day.weekday()) in self.days and candidate > moment:
                return candidate
        raise AssertionError("A non-empty set of weekdays recurs within 8 days")


class PushTarget(BaseModel):
    """Where results are sent, the key to send with, and whether to send after runs."""

    model_config = ConfigDict(frozen=True)

    url: SiteUrl | None = None
    key: SecretStr | None = None
    auto: bool = False


class TargetUpdate(BaseModel):
    """A change to the push target from the browser.

    A key of None keeps the saved key, unless the URL changes or forget_key is set:
    a key is never sent to a site other than the one it was saved for.
    """

    model_config = ConfigDict(frozen=True)

    url: SiteUrl | None = None
    key: SecretStr | None = None
    forget_key: bool = False
    auto: bool = False

    @model_validator(mode="after")
    def _key_not_blank(self) -> "TargetUpdate":
        if self.key is not None and not self.key.get_secret_value().strip():
            raise ValueError("The key is blank")
        if self.key is not None and self.forget_key:
            raise ValueError("Give a new key or forget the saved one, not both")
        return self

    def apply(self, current: PushTarget) -> PushTarget:
        """The target after this change."""
        if self.key is not None:
            key = SecretStr(self.key.get_secret_value().strip())
        elif self.forget_key or self.url != current.url:
            key = None
        else:
            key = current.key
        return PushTarget(url=self.url, key=key, auto=self.auto)


class ControlPreferences(BaseModel):
    """Everything saved in data_dir/control.json."""

    model_config = ConfigDict(frozen=True)

    schedule: Schedule = Schedule()
    target: PushTarget = PushTarget()


class PreferencesStore:
    """The current preferences, saved to disk on every change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._value = _load(path)

    @property
    def value(self) -> ControlPreferences:
        with self._lock:
            return self._value

    def replace(self, value: ControlPreferences) -> None:
        """Write value to disk, then make it current."""
        data = value.model_dump(mode="json")
        key = value.target.key
        data["target"]["key"] = key.get_secret_value() if key else None
        with self._lock:
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(temporary, self._path)
            self._value = value


def _load(path: Path) -> ControlPreferences:
    if not path.exists():
        return ControlPreferences()
    try:
        return ControlPreferences.model_validate_json(path.read_bytes())
    except ValidationError as error:
        raise ValueError(f"{path} is not valid: {error}") from error


class Source(StrEnum):
    """Where an effective push setting came from."""

    SAVED = "saved"
    CONFIG = "config"
    ENVIRONMENT = "environment"


@dataclass(frozen=True)
class ResolvedTarget:
    """The URL and key a push would use now, and where each came from."""

    url: str | None
    url_source: Source | None
    key: str | None
    key_source: Source | None


def resolve(
    target: PushTarget, settings: Settings, environ: Mapping[str, str]
) -> ResolvedTarget:
    """Saved values win; otherwise [site] url from beacon.toml and the key variable."""
    if target.url is not None:
        url, url_source = target.url, Source.SAVED
    elif settings.site.url is not None:
        url, url_source = settings.site.url, Source.CONFIG
    else:
        url, url_source = None, None
    if target.key is not None:
        key, key_source = target.key.get_secret_value(), Source.SAVED
    elif environ.get(runner.SITE_KEY_ENV):
        key, key_source = environ[runner.SITE_KEY_ENV], Source.ENVIRONMENT
    else:
        key, key_source = None, None
    return ResolvedTarget(url, url_source, key, key_source)


class TargetPusher:
    """Pushes the newest bundles to whatever target is current when called."""

    def __init__(
        self,
        settings: Settings,
        preferences: PreferencesStore,
        environ: Mapping[str, str] = os.environ,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._preferences = preferences
        self._environ = environ
        self._transport = transport

    def __call__(self, committees: Sequence[str] | None) -> PushOutcome:
        target = resolve(self._preferences.value.target, self._settings, self._environ)
        if target.url is None:
            raise PushError("Choose a site to send results to")
        if target.key is None:
            raise PushError(f"Enter the site's ingest key or set {runner.SITE_KEY_ENV}")
        with site_client(target.url, target.key, transport=self._transport) as client:
            return runner.push(self._settings, client, committees)
