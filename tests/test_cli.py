"""The command line: call, doctor, simulate's parser, and settings."""

from __future__ import annotations

import json
import socket

import pytest

from steam_backlog_mcp import cli, config, doctor
from steam_backlog_mcp.sim.fake_backlog import FakeBacklog


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    out = capsys.readouterr()
    return int(exc.value.code or 0), out.out, out.err


def test_call_lists_and_calls_tools(fake: FakeBacklog, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["call", "tools"], capsys)
    assert code == 0 and "backlog_suggest_picks" in out
    code, out, _ = run(["call", "suggest_picks", "count=2"], capsys)
    assert code == 0 and len(json.loads(out)["picks"]) == 2
    code, out, _ = run(["call", "overview"], capsys)  # backlog_get_overview by its short name
    assert code == 0 and json.loads(out)["games"] == 49
    code, out, _ = run(["call", "set_lane", 'games=["Hades"]', "lane=done", "--dry-run"], capsys)
    assert code == 0 and json.loads(out)["outcome"] == "dry_run" and fake.writes() == []


def test_call_errors(fake: FakeBacklog, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["call", "nope"], capsys)[0] == 2
    assert run(["call", "find_games", "notkeyvalue"], capsys)[0] == 2
    code, _, err = run(["call", "reorder_next"], capsys)
    assert code == 1 and "either game and position" in err


def test_doctor_all_good(
    fake: FakeBacklog, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    monkeypatch.setenv("MCP_HTTP_PORT", str(s.getsockname()[1]))
    s.close()
    code, out, _ = run(["doctor"], capsys)
    assert code == 0, out
    assert "API 2: every contract feature present" in out and "49 games, schema 2" in out
    code, out, _ = run(["doctor", "--json"], capsys)
    data = json.loads(out)
    assert data["ok"] and data["api"] == 2 and all(data["features"].values())


def test_doctor_on_api_1(fake_v1: FakeBacklog, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(["check"], capsys)
    assert code == 0  # older server: a warning, not a failure
    assert "[warn] api" in out and "missing library_etag" in out and "git pull" in out


def test_doctor_unreachable(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKLOG_URL", "http://127.0.0.1:9")
    code, out, _ = run(["doctor"], capsys)
    assert code == 1 and "[FAIL] reach" in out


def test_doctor_no_export_and_settings(
    fake: FakeBacklog, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.library = None
    fake.export_configured = False
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "example.cloudflareaccess.com")
    monkeypatch.setenv("MQTT_PASSWORD", "hunter2-secret")
    code, out, _ = run(["doctor"], capsys)
    assert code == 1 and "[FAIL] library" in out and "[FAIL] access" in out
    assert "no export command configured" in out
    assert "MQTT_PASSWORD" in out and "hunter2-secret" not in out  # names only, never values
    monkeypatch.setenv("CF_ACCESS_AUD", "aud")
    fake.library = {"games": [], "schema": 2, "exportedAt": "2020-01-01T00:00:00+00:00"}
    code, out, _ = run(["doctor"], capsys)
    assert "MCP_PUBLIC_HOSTS is empty" in out and "over two weeks old" in out
    monkeypatch.setenv("MCP_PUBLIC_HOSTS", "mcp.example.com")
    _, out, _ = run(["doctor"], capsys)
    assert "Cloudflare Access settings complete" in out


def test_doctor_port_checks() -> None:
    assert doctor._port_check({"MCP_HTTP_PORT": "x"}).ok is False
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    busy = doctor._port_check({"MCP_HTTP_PORT": str(s.getsockname()[1])})
    s.close()
    assert busy.ok and busy.warn


def test_settings() -> None:
    s = config.load_settings({"BACKLOG_URL": "backlog.lan", "BACKLOG_DRY_RUN": "yes", "BACKLOG_WRITE_RATE": "9999"})
    assert (s.url, s.dry_run, s.write_rate) == ("http://backlog.lan:8484", True, 600)
    assert config.load_settings({"BACKLOG_WRITE_RATE": "lots"}).write_rate == 60
    assert config.load_settings({}).url == "http://127.0.0.1:8484"
    assert config.normalize_url("https://backlog.example.com/") == "https://backlog.example.com"
    assert config.normalize_url("[::1]") == "http://[::1]:8484"
    with pytest.raises(ValueError):
        config.normalize_url("ftp://x")


def test_parser_has_every_command() -> None:
    p = cli.build_parser()
    assert p.parse_args(["simulate", "--api", "1"]).api == 1
    assert p.parse_args(["serve", "--dry-run"]).dry_run
    assert cli._parse_args(["count=3", "q=hollow"]) == {"count": 3, "q": "hollow"}
