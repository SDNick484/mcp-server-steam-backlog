"""doctor's MQTT checks for now playing (N4): login, subscription, a retained state within 5 seconds,
and with --probe-acl, whether the login can publish."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from steam_backlog_mcp import cli, doctor
from steam_backlog_mcp.sim.fake_backlog import FakeBacklog

from .fake_mqtt import FakeBroker

PASSWORD = "doctor-pw-31337"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    out = capsys.readouterr()
    return int(exc.value.code or 0), out.out + out.err


@pytest.fixture
def broker(fake: FakeBacklog, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeBroker]:
    with FakeBroker(user="reader", password=PASSWORD) as b:
        for k, v in {"MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(b.port), "MQTT_USER": "reader"}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("MQTT_PASSWORD", PASSWORD)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        monkeypatch.setenv("MCP_HTTP_PORT", str(s.getsockname()[1]))
        s.close()
        monkeypatch.setattr(doctor, "RETAINED_WAIT_S", 0.5)
        monkeypatch.setattr(doctor, "PROBE_WAIT_S", 0.5)
        yield b


def playing(b: FakeBroker) -> None:
    b.publish("deck-ha-bridge/steamdeck/state", (FIXTURES / "state_playing.json").read_text(encoding="utf-8"))
    b.publish("deck-ha-bridge/steamdeck/availability", "online")


def test_all_good_without_the_probe(broker: FakeBroker, capsys: pytest.CaptureFixture[str]) -> None:
    playing(broker)
    code, out = run(["doctor"], capsys)
    assert code == 0, out
    assert "[ok  ] mqtt     logged in" in out
    assert "[ok  ] mqtt     allowed to read deck-ha-bridge/+/state and deck-ha-bridge/+/availability" in out
    assert "[ok  ] mqtt     retained state from 1 Deck: steamdeck (online, playing)" in out
    assert "acl" not in out and PASSWORD not in out
    assert broker.client_publishes == []  # without --probe-acl, doctor publishes nothing


def test_the_probe_on_a_read_only_login(broker: FakeBroker, capsys: pytest.CaptureFixture[str]) -> None:
    playing(broker)
    code, out = run(["doctor", "--probe-acl"], capsys)
    assert code == 0, out
    assert "[ok  ] acl" in out and "read-only" in out
    ((topic, payload, retain),) = broker.client_publishes
    assert topic.startswith("deck-ha-bridge/acl-probe/") and payload == b"" and retain is False


def test_the_probe_on_a_login_that_can_publish_is_a_warning(
    broker: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    playing(broker)
    broker.read_only = False
    code, out = run(["doctor", "--probe-acl"], capsys)
    assert code == 0, out  # a warning, not a failure
    assert "[warn] acl" in out and "this login can publish; the ACL is not read-only" in out


def test_a_broker_that_disconnects_on_the_probe_is_read_only(
    broker: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    playing(broker)
    broker.close_on_publish = True
    code, out = run(["doctor", "--probe-acl"], capsys)
    assert code == 0, out
    assert "[ok  ] acl" in out and "closed the connection on the test publish" in out


def test_the_probe_cant_tell_without_read_access_to_its_topic(
    broker: FakeBroker, capsys: pytest.CaptureFixture[str]
) -> None:
    playing(broker)
    broker.deny_subscribe.add("deck-ha-bridge/acl-probe/#")
    code, out = run(["doctor", "--probe-acl"], capsys)
    assert code == 0 and "[warn] acl" in out and "can't tell" in out
    assert broker.client_publishes == []


def test_a_wrong_password_fails(broker: FakeBroker, capsys: pytest.CaptureFixture[str]) -> None:
    broker.password = "something-else"
    code, out = run(["doctor"], capsys)
    assert code == 1 and "[FAIL] mqtt" in out and "refused the login" in out and "MQTT_PASSWORD" in out
    assert PASSWORD not in out


def test_an_unreachable_broker_fails(
    broker: FakeBroker, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MQTT_PORT", "1")
    code, out = run(["doctor", "--probe-acl"], capsys)
    assert code == 1 and "[FAIL] mqtt" in out and "can't reach" in out


def test_a_refused_subscription_fails(broker: FakeBroker, capsys: pytest.CaptureFixture[str]) -> None:
    broker.deny_subscribe.add("deck-ha-bridge/+/state")
    code, out = run(["doctor"], capsys)
    assert code == 1 and "[FAIL] mqtt" in out and "topic read deck-ha-bridge/#" in out


def test_no_retained_state_fails_with_a_hint(broker: FakeBroker, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(["doctor"], capsys)
    assert code == 1 and "[FAIL] mqtt     no retained state" in out and "deck-ha-bridge" in out


def test_json_has_the_mqtt_checks(broker: FakeBroker, capsys: pytest.CaptureFixture[str]) -> None:
    playing(broker)
    code, out = run(["doctor", "--json", "--probe-acl"], capsys)
    data = json.loads(out)
    layers = [c["layer"] for c in data["checks"]]
    assert code == 0 and layers.count("mqtt") == 3 and layers.count("acl") == 1


def test_without_mqtt_host_there_is_nothing_to_check(fake: FakeBacklog, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(["doctor", "--probe-acl"], capsys)
    assert "mqtt" not in out.replace("MQTT_HOST", "")
    assert "[warn] acl" in out and "MQTT_HOST isn't set" in out
