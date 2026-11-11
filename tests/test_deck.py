"""Phase 2, now playing on the Steam Deck: settings, the MQTT watcher and its cache.

The Deck bridge (steam-deck-ha-bridge) publishes its state as retained JSON on
deck-ha-bridge/<node>/state and online/offline on deck-ha-bridge/<node>/availability. The contract
is that repo's docs/state-schema.md; tests/fixtures/state_playing.json and state_idle.json are
copied from its tests/fixtures (produced by the bridge's own code).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from steam_backlog_mcp import config
from steam_backlog_mcp.logsafe import redact_secrets

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
