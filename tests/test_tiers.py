"""This server's tiers, which the shared test can't fully see.

The shared test_every_tool_has_a_tier reads the module-level server, which leaves out
backlog_now_playing: lifespan() registers it only when MQTT_HOST is set. Here it is registered on
a server of its own, and every tool's tier is pinned so a change to one is a decision, not a drift.
None is costly: every write returns what it replaced.
"""

from __future__ import annotations

import pytest
from mcp.server import MCPServer

from steam_backlog_mcp import remote, server

from .test_authorization import costly_without_confirm, tool_functions, untiered_tools

pytestmark = pytest.mark.anyio

READ = {
    "backlog_get_overview",
    "backlog_find_games",
    "backlog_get_stats",
    "backlog_get_game",
    "backlog_suggest_picks",
    "backlog_get_next_queue",
    "backlog_get_export_status",
    "backlog_now_playing",
}
EVERYDAY = {"backlog_set_lane", "backlog_set_note", "backlog_reorder_next", "backlog_start_export"}


async def test_every_tool_including_now_playing_has_the_expected_tier():
    m = MCPServer("tiers")
    for register in (*server.TOOL_GROUPS, server.register_now_playing):
        register(m)
    assert await untiered_tools(m) == []
    assert await costly_without_confirm(m) == []
    tiers = {}
    for name, fn in (await tool_functions(m)).items():
        spec = remote.tier_of(fn)
        assert spec is not None
        tiers[name] = spec.tier
    assert tiers == {**dict.fromkeys(READ, "read"), **dict.fromkeys(EVERYDAY, "everyday")}
