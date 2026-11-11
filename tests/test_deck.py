"""Phase 2, now playing on the Steam Deck: settings, the MQTT watcher and its cache.

The Deck bridge (steam-deck-ha-bridge) publishes its state as retained JSON on
deck-ha-bridge/<node>/state and online/offline on deck-ha-bridge/<node>/availability. The contract
is that repo's docs/state-schema.md; tests/fixtures/state_playing.json and state_idle.json are
copied from its tests/fixtures (produced by the bridge's own code).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from steam_backlog_mcp import config, deck
from steam_backlog_mcp.logsafe import redact_secrets
from steam_backlog_mcp.tasks import spawn_task, stop_task

from .fake_mqtt import FakeBroker

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


# --------------------------------------------------------------------------- settings (N3)
def test_mqtt_is_off_without_a_host() -> None:
    assert config.load_settings({}).mqtt is None
    s = config.load_settings({"MQTT_USER": "reader", "MQTT_PASSWORD": "pw-123456"})
    assert s.mqtt is None
    assert any("MQTT_HOST" in p for p in s.mqtt_problems)


def test_mqtt_settings_and_defaults() -> None:
    m = config.load_settings({"MQTT_HOST": "broker.example.com", "MQTT_USER": "reader"}).mqtt
    assert m is not None
    assert (m.host, m.port, m.user, m.password, m.tls) == ("broker.example.com", 1883, "reader", None, False)
    assert m.state_topic == "deck-ha-bridge/+/state"
    assert m.availability_topic == "deck-ha-bridge/+/availability"
    assert m.base == "deck-ha-bridge"
    tls = config.load_settings({"MQTT_HOST": "b", "MQTT_TLS": "1"}).mqtt
    assert tls is not None and (tls.tls, tls.port) == (True, 8883)
    custom = config.load_settings({"MQTT_HOST": "b", "MQTT_PORT": "2883", "DECK_STATE_TOPIC": "decks/one/state"}).mqtt
    assert custom is not None and custom.port == 2883
    assert (custom.availability_topic, custom.base) == ("decks/one/availability", "decks")


@pytest.mark.parametrize(
    ("env", "problem"),
    [
        ({"MQTT_PORT": "0"}, "MQTT_PORT"),
        ({"MQTT_PORT": "lots"}, "MQTT_PORT"),
        ({"DECK_STATE_TOPIC": "deck-ha-bridge/#"}, "DECK_STATE_TOPIC"),
        ({"DECK_STATE_TOPIC": "deck-ha-bridge/+/status"}, "DECK_STATE_TOPIC"),
        ({"DECK_STATE_TOPIC": "deck-ha-bridge/st+/state"}, "DECK_STATE_TOPIC"),
        ({"DECK_STATE_TOPIC": "+/x/state"}, "DECK_STATE_TOPIC"),
    ],
)
def test_bad_mqtt_values_fall_back_with_a_problem(env: dict[str, str], problem: str) -> None:
    s = config.load_settings({"MQTT_HOST": "b", **env})
    assert s.mqtt is not None
    assert (s.mqtt.port, s.mqtt.state_topic) == (1883, "deck-ha-bridge/+/state")
    assert any(problem in p for p in s.mqtt_problems)


def test_mqtt_names_are_known_not_extra() -> None:
    s = config.load_settings({"MQTT_HOST": "b", "MQTT_TLS": "0", "DECK_STATE_TOPIC": "a/+/state", "BACKLOG_X": "1"})
    assert set(s.extra) == {"BACKLOG_X"}


def test_the_password_is_a_registered_secret_and_never_in_repr() -> None:
    s = config.load_settings({"MQTT_HOST": "b", "MQTT_PASSWORD": "correct-horse-42"})
    assert "correct-horse-42" not in repr(s)
    assert "correct-horse-42" not in redact_secrets("login failed for reader / correct-horse-42 at broker")


def test_a_lan_broker_address_is_masked_in_logs() -> None:
    s = config.load_settings({"MQTT_HOST": "192.0.2.7"}).mqtt
    assert s is not None and deck.DeckWatcher(s).endpoint == "x.x.x.7:1883"


# --------------------------------------------------------------------------- the cache (N1, N6)
STATE = "deck-ha-bridge/steamdeck/state"
AVAIL = "deck-ha-bridge/steamdeck/availability"


def watcher(**env: str) -> deck.DeckWatcher:
    s = config.load_settings({"MQTT_HOST": "127.0.0.1", **env}).mqtt
    assert s is not None
    return deck.DeckWatcher(s)


def raw(name: str, **changes: Any) -> bytes:
    return json.dumps({**fixture(name), **changes}).encode()


def test_a_playing_state_is_cached_per_device() -> None:
    w = watcher()
    w.connected_now()
    w.handle(STATE, raw("state_playing.json"))
    (d,) = w.snapshot()
    assert d.device == "steamdeck" and d.problem is None and d.fresh
    assert d.state is not None
    assert (d.state.playing, d.state.appid, d.state.game, d.state.mode) == (True, 367520, "Hollow Knight", "Game Mode")
    assert d.state.started == "2023-11-14T22:33:54+00:00"
    assert d.online is None  # no availability message yet


def test_idle_state_has_no_game() -> None:
    w = watcher()
    w.handle(STATE, raw("state_idle.json"))
    (d,) = w.snapshot()
    assert d.state is not None and d.state.playing is False and d.state.appid is None


def test_two_decks_are_kept_apart() -> None:
    w = watcher()
    w.handle(STATE, raw("state_playing.json"))
    w.handle("deck-ha-bridge/lcd_deck/state", raw("state_idle.json"))
    w.handle("deck-ha-bridge/lcd_deck/availability", b"offline")
    by = {d.device: d for d in w.snapshot()}
    assert set(by) == {"steamdeck", "lcd_deck"}
    assert by["steamdeck"].state.playing and not by["lcd_deck"].state.playing  # type: ignore[union-attr]
    assert by["lcd_deck"].online is False and by["steamdeck"].online is None


def test_availability_flips() -> None:
    w = watcher()
    w.handle(AVAIL, b"online")
    assert w.snapshot()[0].online is True
    w.handle(AVAIL, b"offline")
    assert w.snapshot()[0].online is False
    w.handle(AVAIL, b"something else")
    assert w.snapshot()[0].online is None
    w.handle(AVAIL, b"online")
    assert w.snapshot()[0].online is True


@pytest.mark.parametrize(
    "payload",
    [
        b"{not json",
        b"[1, 2]",
        b"\xff\xfe",
        json.dumps({**fixture("state_playing.json"), "appid": "367520"}).encode(),
        json.dumps({**fixture("state_playing.json"), "playing": True}).encode(),
        json.dumps({**fixture("state_playing.json"), "game": None}).encode(),
        json.dumps({k: v for k, v in fixture("state_playing.json").items() if k != "game"}).encode(),
        b"{" + b'"pad": "' + b"x" * 20000 + b'"}',
    ],
)
def test_a_malformed_state_is_a_problem_and_keeps_the_last_good_one(payload: bytes) -> None:
    w = watcher()
    w.handle(STATE, raw("state_playing.json"))
    w.handle(STATE, payload)
    (d,) = w.snapshot()
    assert d.problem and "state" in d.problem
    assert d.state is not None and d.state.game == "Hollow Knight"  # the last good one
    w.handle(STATE, raw("state_idle.json"))
    assert w.snapshot()[0].problem is None


def test_a_malformed_first_message_leaves_no_state() -> None:
    w = watcher()
    w.handle(STATE, b"nope")
    (d,) = w.snapshot()
    assert d.state is None and d.problem


def test_unknown_fields_are_ignored_and_only_what_we_use_is_kept() -> None:
    w = watcher()
    w.handle(STATE, raw("state_playing.json", future_field=[1, 2, 3]))
    (d,) = w.snapshot()
    assert d.problem is None
    assert set(vars(d.state)) == {"mode", "playing", "appid", "game", "started"}


def test_an_empty_retained_payload_forgets_the_device() -> None:
    w = watcher()
    w.handle(STATE, raw("state_playing.json"))
    w.handle(AVAIL, b"online")
    w.handle(STATE, b"")
    w.handle(AVAIL, b"")
    assert w.snapshot() == []


def test_other_topics_are_ignored() -> None:
    w = watcher()
    for t in ("deck-ha-bridge/steamdeck/other", "homeassistant/status", "deck-ha-bridge/a/b/state"):
        w.handle(t, raw("state_playing.json"))
    assert w.snapshot() == []


def test_a_custom_single_device_topic() -> None:
    w = watcher(DECK_STATE_TOPIC="decks/den/state")
    w.handle("decks/den/state", raw("state_playing.json"))
    w.handle("decks/den/availability", b"online")
    (d,) = w.snapshot()
    assert d.device == "den" and d.online


def test_the_number_of_devices_is_capped() -> None:
    w = watcher()
    for i in range(deck.MAX_DECKS + 5):
        w.handle(f"deck-ha-bridge/d{i}/state", raw("state_idle.json"))
    assert len(w.snapshot()) == deck.MAX_DECKS


def test_messages_from_an_earlier_connection_are_not_fresh() -> None:
    w = watcher()
    w.connected_now()
    w.handle(STATE, raw("state_playing.json"))
    assert w.snapshot()[0].fresh
    w.disconnected()
    assert not w.snapshot()[0].fresh
    w.connected_now()
    assert not w.snapshot()[0].fresh  # nothing since this connection yet
    w.handle(AVAIL, b"online")
    assert w.snapshot()[0].fresh


# --------------------------------------------------------------------------- the connection (N1)
@pytest.fixture
def broker() -> Iterator[FakeBroker]:
    with FakeBroker(user="reader", password="pw-for-tests") as b:
        yield b


def live(b: FakeBroker, **kw: Any) -> deck.DeckWatcher:
    s = config.load_settings(
        {"MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(b.port), "MQTT_USER": "reader", "MQTT_PASSWORD": "pw-for-tests"}
    ).mqtt
    assert s is not None
    return deck.DeckWatcher(s, **kw)


async def eventually(check: Any, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.02)


@pytest.mark.anyio
async def test_retained_state_arrives_on_subscribe_and_nothing_is_published(broker: FakeBroker) -> None:
    broker.publish(STATE, raw("state_playing.json"))
    broker.publish(AVAIL, "online")
    w = live(broker)
    task = spawn_task(w.run(), name="deck watch")
    try:
        assert await w.wait_ready(3.0)
        (d,) = w.snapshot()
        assert d.online and d.fresh and d.state.game == "Hollow Knight"  # type: ignore[union-attr]
        assert w.connected and w.status == "connected"
        assert sorted(broker.subscriptions()) == ["deck-ha-bridge/+/availability", "deck-ha-bridge/+/state"]
        broker.publish(STATE, raw("state_idle.json"))  # a live update after subscribing
        await eventually(lambda: not w.snapshot()[0].state.playing)  # type: ignore[union-attr]
    finally:
        await stop_task(task, 3.0, "deck watch")
    assert broker.client_publishes == []  # N1: the server never publishes
    assert all(c["will"] is None for c in broker.connects)  # not even a last will


@pytest.mark.anyio
async def test_it_reconnects_after_the_broker_drops_and_gets_the_state_again(broker: FakeBroker) -> None:
    broker.publish(STATE, raw("state_playing.json"))
    w = live(broker, backoff_initial=0.05)
    task = spawn_task(w.run(), name="deck watch")
    try:
        assert await w.wait_ready(3.0)
        broker.drop()
        await eventually(lambda: len(broker.connects) == 2 and w.connected and w.snapshot()[0].fresh)
        assert w.snapshot()[0].state.game == "Hollow Knight"  # type: ignore[union-attr]
    finally:
        await stop_task(task, 3.0, "deck watch")
    assert not w.connected


@pytest.mark.anyio
async def test_a_wrong_password_is_reported_and_retried(broker: FakeBroker) -> None:
    broker.password = "something-else"
    w = live(broker, backoff_initial=0.05)
    task = spawn_task(w.run(), name="deck watch")
    try:
        assert await w.wait_ready(3.0)
        await eventually(lambda: len(broker.connects) >= 2)
        assert w.status == "login_refused" and not w.connected
    finally:
        await stop_task(task, 3.0, "deck watch")


@pytest.mark.anyio
async def test_a_refused_subscription_is_reported(broker: FakeBroker) -> None:
    broker.deny_subscribe.add("deck-ha-bridge/+/state")
    w = live(broker, backoff_initial=0.05)
    task = spawn_task(w.run(), name="deck watch")
    try:
        assert await w.wait_ready(3.0)
        await eventually(lambda: w.status == "subscribe_refused")
        assert not w.connected
    finally:
        await stop_task(task, 3.0, "deck watch")


@pytest.mark.anyio
async def test_backoff_doubles_and_is_capped() -> None:
    s = config.load_settings({"MQTT_HOST": "127.0.0.1", "MQTT_PORT": "1"}).mqtt
    assert s is not None
    delays: list[float] = []

    class Done(Exception):
        pass

    async def sleep(d: float) -> None:
        delays.append(d)
        if len(delays) == 8:
            raise Done

    w = deck.DeckWatcher(s, backoff_initial=1.0, backoff_max=30.0, sleep=sleep)
    with pytest.raises(Done):
        await w.run()
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert w.status == "unreachable" and w.ready.is_set()


@pytest.mark.anyio
async def test_backoff_starts_over_after_a_good_connection(broker: FakeBroker) -> None:
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep(d: float) -> None:
        delays.append(d)
        await real_sleep(0.01)

    w = live(broker, backoff_initial=0.5, sleep=sleep)
    task = spawn_task(w.run(), name="deck watch")
    try:
        assert await w.wait_ready(3.0)
        for n in (2, 3):
            broker.drop()
            await eventually(lambda n=n: len(broker.connects) == n and w.connected)
    finally:
        await stop_task(task, 3.0, "deck watch")
    assert delays == [0.5, 0.5]


@pytest.mark.anyio
async def test_wait_ready_gives_up(broker: FakeBroker) -> None:
    w = live(broker)  # never started
    assert await w.wait_ready(0.05) is False
