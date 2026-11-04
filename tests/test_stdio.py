"""End to end: the installed entry point over stdio, against the fake backlog server."""

from __future__ import annotations

import os
import shutil
import sys

import pytest
from mcp import Client, StdioServerParameters

from steam_backlog_mcp.sim.fake_backlog import FakeBacklog

from .test_tools import ALL_TOOLS

pytestmark = pytest.mark.anyio


def _exe() -> str:
    exe = shutil.which("mcp-server-steam-backlog", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("entry point not installed (pip install -e .)")
    return exe


async def test_serve_over_stdio(fake: FakeBacklog) -> None:
    env = {**os.environ, "BACKLOG_URL": fake.url}
    async with Client(StdioServerParameters(command=_exe(), args=[], env=env)) as c:
        assert {t.name for t in (await c.list_tools()).tools} == ALL_TOOLS
        o = (await c.call_tool("backlog_get_overview", {})).structured_content
        assert o is not None and o["games"] == 49


async def test_stdio_with_nothing_listening() -> None:
    env = {**os.environ, "BACKLOG_URL": "http://127.0.0.1:9"}
    async with Client(StdioServerParameters(command=_exe(), args=[], env=env)) as c:
        r = await c.call_tool("backlog_get_overview", {})
        assert r.is_error and "isn't responding" in r.content[0].text  # type: ignore[union-attr]
