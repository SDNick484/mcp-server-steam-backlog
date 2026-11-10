"""No SteamID, persona, LAN address or local path leaves through a tool result or a log line.

The export file carries the user's SteamID and Steam display name; the MCP drops both when it
parses the library. This drives every tool against a library whose identity fields are set to
recognizable values and greps everything that came back, logs included.

The SteamID below is shaped like a SteamID64 but below the range Steam issues to accounts, so it
can't be anyone's.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import pytest
from mcp import Client

from steam_backlog_mcp import server
from steam_backlog_mcp.logsafe import RedactingFormatter
from steam_backlog_mcp.sim.fake_backlog import FIXTURE, FakeBacklog

pytestmark = pytest.mark.anyio

STEAMID = "7656119" + "0000000001"
PERSONA = "Zebulon Quartermaine"
FORBIDDEN = [STEAMID, PERSONA, "/home/", "192.0.2."]


@pytest.fixture
def identified(monkeypatch: pytest.MonkeyPatch) -> Any:
    lib = json.loads(FIXTURE.read_text(encoding="utf-8"))
    lib["steamid"], lib["persona"] = STEAMID, PERSONA
    with FakeBacklog(lib) as f:
        f.export_step_s = 0.01
        monkeypatch.setenv("BACKLOG_URL", f.url)
        yield f


CALLS: list[tuple[str, dict[str, Any]]] = [
    ("backlog_get_overview", {}),
    ("backlog_find_games", {"limit": 100}),
    ("backlog_find_games", {"query": "zzzz"}),
    ("backlog_get_game", {"game": "620"}),
    ("backlog_get_game", {"game": "DOOM"}),
    ("backlog_suggest_picks", {"count": 20}),
    ("backlog_get_next_queue", {}),
    ("backlog_set_lane", {"games": ["620"], "lane": "next"}),
    ("backlog_set_note", {"game": "620", "note": "hello"}),
    ("backlog_reorder_next", {"order": ["620"]}),
    ("backlog_start_export", {}),
    ("backlog_get_export_status", {}),
    ("backlog_get_stats", {"dimension": "publisher"}),
    ("backlog_get_stats", {"dimension": "tags", "backlog_only": True}),
]


async def test_nothing_personal_in_any_result_or_log(identified: FakeBacklog, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    out: list[str] = []
    async with Client(server.mcp) as c:
        for name, args in CALLS:
            r = await c.call_tool(name, args)
            out.append(json.dumps(r.structured_content) + " ".join(getattr(p, "text", "") for p in r.content))
        time.sleep(0.3)  # let the simulated export finish, then read its lines
        r = await c.call_tool("backlog_get_export_status", {})
        out.append(json.dumps(r.structured_content))
        out.append((await c.read_resource("backlog://game/620")).contents[0].text)  # type: ignore[union-attr]
    fmt = RedactingFormatter("%(message)s")
    logs = "\n".join(fmt.format(rec) for rec in caplog.records)
    blob = "\n".join(out)
    for needle in FORBIDDEN:
        assert needle not in blob, needle
        assert needle not in logs, needle
    assert not re.search(r"7656119\d{10}", blob + logs)


async def test_steamid_in_a_log_message_is_redacted_even_unredacted() -> None:
    """The SteamID rule is registered as a secret: it applies with --no-redact too."""
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, f"export for {STEAMID} done", None, None)
    assert STEAMID not in RedactingFormatter("%(message)s", addresses=False).format(rec)
    assert "7656119XXXXXXXXXX" in RedactingFormatter("%(message)s").format(rec)


async def test_export_lines_are_redacted_again(identified: FakeBacklog) -> None:
    """Belt and braces: even if a backlog server forgot §4.8, the MCP's own pass catches SteamIDs and IPs."""
    st = server._status({"state": "running", "lines": [f"{STEAMID}: 49 games", "HTTP 429 from 192.0.2.20"]})
    assert st.lines == ["7656119XXXXXXXXXX: 49 games", "HTTP 429 from x.x.x.20"]
