"""Saved schedule and push target: timing, key handling, and pushing through them."""

import json
from datetime import UTC, datetime, time
from pathlib import Path

import httpx
import pytest
from beacon_contract import v1
from pydantic import SecretStr, ValidationError

from beacon.adapters.site_push import CHANGELOG_PATH, PushError
from beacon.config import Settings, SiteSettings
from beacon.domain.local_time import LEGISLATURE_TZ
from beacon.gui.control import (
    ControlPreferences,
    PreferencesStore,
    PushTarget,
    Schedule,
    Source,
    TargetPusher,
    TargetUpdate,
    Weekday,
    resolve,
)
from tests.reporters.test_site_bundle import FakeSite


def _eastern(*args: int) -> datetime:
    return datetime(*args, tzinfo=LEGISLATURE_TZ)


def test_next_run_is_later_today_or_on_the_next_chosen_day():
    daily = Schedule(enabled=True, at=time(1, 0))
    assert daily.next_after(_eastern(2026, 11, 20, 0, 30)) == _eastern(
        2026, 11, 20, 1, 0
    )
    assert daily.next_after(_eastern(2026, 11, 20, 1, 0)) == _eastern(
        2026, 11, 21, 1, 0
    )
    weekdays = daily.model_copy(update={"days": frozenset({Weekday.MONDAY})})
    friday = _eastern(2026, 11, 20, 9, 0)
    assert weekdays.next_after(friday) == _eastern(2026, 11, 23, 1, 0)
    assert Schedule(enabled=False).next_after(friday) is None


def test_next_run_is_state_house_time_across_daylight_saving():
    daily = Schedule(enabled=True, at=time(1, 0))
    before = daily.next_after(datetime(2026, 10, 31, 12, tzinfo=UTC))
    after = daily.next_after(datetime(2026, 11, 1, 12, tzinfo=UTC))
    assert before is not None and after is not None
    assert before.astimezone(UTC).hour == 5 and after.astimezone(UTC).hour == 6


@pytest.mark.parametrize(
    "fields",
    [{"days": []}, {"plan": "nope"}, {"at": "01:00:00+00:00"}],
    ids=["no days", "unknown plan", "zoned time"],
)
def test_invalid_schedule_rejected(fields: dict):
    with pytest.raises(ValidationError):
        Schedule.model_validate(fields)


def test_key_is_kept_only_for_the_site_it_was_saved_for():
    saved = PushTarget(url="https://a.example", key=SecretStr("k1"))
    kept = TargetUpdate(url="https://a.example", auto=True).apply(saved)
    assert kept.key == SecretStr("k1") and kept.auto
    moved = TargetUpdate(url="https://b.example").apply(saved)
    assert moved.key is None
    forgot = TargetUpdate(url="https://a.example", forget_key=True).apply(saved)
    assert forgot.key is None
    replaced = TargetUpdate(url="https://b.example", key=SecretStr(" k2 ")).apply(saved)
    assert replaced.key == SecretStr("k2")


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "http://tracker.example.org"},
        {"url": "ftp://127.0.0.1"},
        {"url": "https://a.example/?x=1"},
        {"url": "https://a.example", "key": "  "},
        {"url": "https://a.example", "key": "k", "forget_key": True},
    ],
    ids=["plain http off-machine", "scheme", "query", "blank key", "key and forget"],
)
def test_invalid_target_rejected(fields: dict):
    with pytest.raises(ValidationError):
        TargetUpdate.model_validate(fields)


def test_plain_http_allowed_on_this_machine():
    update = TargetUpdate(url="http://127.0.0.1:5000/")
    assert update.url == "http://127.0.0.1:5000"


def test_preferences_round_trip_and_key_never_dumped(tmp_path: Path):
    path = tmp_path / "control.json"
    store = PreferencesStore(path)
    assert store.value == ControlPreferences()
    value = ControlPreferences(
        schedule=Schedule(enabled=True, days=frozenset({Weekday.SUNDAY})),
        target=PushTarget(url="https://a.example", key=SecretStr("secret"), auto=True),
    )
    store.replace(value)
    assert json.loads(path.read_text())["target"]["key"] == "secret"
    assert PreferencesStore(path).value == value
    assert "secret" not in value.model_dump_json()


def test_corrupt_preferences_name_the_file(tmp_path: Path):
    path = tmp_path / "control.json"
    path.write_text('{"schedule": {"days": []}}')
    with pytest.raises(ValueError, match=r"control.json"):
        PreferencesStore(path)


def test_saved_values_win_over_config_and_environment():
    settings = Settings(site=SiteSettings(url="https://config.example"))
    env = {"BEACON_SITE_KEY": "from-env"}
    fallback = resolve(PushTarget(), settings, env)
    assert (fallback.url, fallback.url_source) == (
        "https://config.example",
        Source.CONFIG,
    )
    assert (fallback.key, fallback.key_source) == ("from-env", Source.ENVIRONMENT)
    saved = resolve(
        PushTarget(url="https://saved.example", key=SecretStr("k")), settings, env
    )
    assert (saved.url_source, saved.key, saved.key_source) == (
        Source.SAVED,
        "k",
        Source.SAVED,
    )
    assert resolve(PushTarget(), Settings(), {}).url is None


def test_pusher_needs_a_site_and_key(tmp_path: Path):
    store = PreferencesStore(tmp_path / "control.json")
    with pytest.raises(PushError, match="Choose a site"):
        TargetPusher(Settings(data_dir=tmp_path), store, {})(None)
    store.replace(ControlPreferences(target=PushTarget(url="https://a.example")))
    with pytest.raises(PushError, match="ingest key"):
        TargetPusher(Settings(data_dir=tmp_path), store, {})(None)


class SiteWithChangelog(FakeSite):
    """FakeSite that also accepts the changelog."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != CHANGELOG_PATH:
            return super().__call__(request)
        self.calls.append((request.method, request.url.path))
        body = v1.Changelog.model_validate_json(request.content)
        state = v1.ChangelogState(
            releases=tuple(r.version for r in body.releases), updated=()
        )
        return httpx.Response(200, content=state.model_dump_json())


def test_pusher_sends_changelog_then_bundles_to_saved_site(populated: Settings):
    store = PreferencesStore(populated.data_dir / "control.json")
    store.replace(
        ControlPreferences(
            target=PushTarget(url="https://site.test", key=SecretStr("k"))
        )
    )
    site = SiteWithChangelog()
    pusher = TargetPusher(populated, store, {}, httpx.MockTransport(site))
    outcome = pusher(None)
    assert outcome.run.status == v1.RunStatus.PUBLISHED
    assert outcome.run.committees == ("J19",)
    assert [c[0] for c in site.calls] == ["PUT", "POST", "PUT", "POST"]
    assert site.calls[0][1] == CHANGELOG_PATH
