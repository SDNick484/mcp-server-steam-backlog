"""This server's tools over Streamable HTTP, behind the Cloudflare Access check.

test_remote.py (shared with the sibling servers) tests the transport with a one-tool server.
This runs *these* tools the way the OpenRC service serves them: uvicorn on a free port, MCP over
HTTP, Access assertions signed with a throwaway RSA key, and the fake backlog behind it.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx2
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from steam_backlog_mcp import cli, remote, server
from steam_backlog_mcp.sim.fake_backlog import FakeBacklog

TEAM = "example.cloudflareaccess.com"
AUD = "backlog-aud"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
POLICY = remote.AccessPolicy(TEAM, (AUD,))
PATH = "/mcp/backlog"


def token(**overrides: object) -> str:
    now = int(time.time())
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "iat": now, "exp": now + 300, "email": "player@example.com"}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


async def public_key(_token: str) -> object:
    return KEY.public_key()


@asynccontextmanager
async def serving() -> AsyncIterator[str]:
    cfg = remote.HttpConfig(path=PATH, access=POLICY)
    app = remote.build_app(server.mcp, cfg, remote.AccessVerifier(POLICY, public_key))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(srv.serve, [sock])
        while not srv.started:
            await anyio.sleep(0.01)
        yield f"http://127.0.0.1:{port}{PATH}"
        srv.should_exit = True


def client(url: str, assertion: str | None) -> Client:
    headers = {remote.ACCESS_HEADER: assertion} if assertion else {}
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers)))


@pytest.mark.anyio
async def test_tools_work_over_http_with_a_valid_assertion(fake: FakeBacklog) -> None:
    async with serving() as url, client(url, token()) as c:
        o = (await c.call_tool("backlog_get_overview", {})).structured_content
        assert o is not None and o["games"] == 49
        r = (await c.call_tool("backlog_set_lane", {"games": ["Hades"], "lane": "done"})).structured_content
        assert r is not None and r["outcome"] == "done"
    assert fake.triage == {"1145360": "done"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "assertion",
    [None, "garbage", token(exp=int(time.time()) - 3600), token(aud=["other-app"]), token(iss="https://evil.example")],
    ids=["missing", "garbage", "expired", "wrong-audience", "wrong-issuer"],
)
async def test_bad_assertions_never_reach_the_backlog(fake: FakeBacklog, assertion: str | None) -> None:
    async with serving() as url, httpx2.AsyncClient() as http:
        headers = {remote.ACCESS_HEADER: assertion} if assertion else {}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "backlog_set_lane", "arguments": {"games": ["620"], "lane": "skip"}}}  # fmt: skip
        resp = await http.post(url, json=body, headers=headers)
        assert (resp.status_code, resp.json()) == (403, {"error": "forbidden"})
    assert fake.requests == []


def test_http_flags_and_defaults() -> None:
    args = cli.build_parser().parse_args(["serve", "--http"])
    cfg = remote.http_config(args, env={})
    assert (args.http, cfg.bind, cfg.port, cfg.path, cfg.access) == (True, "127.0.0.1", 8486, PATH, None)
    env = {"MCP_HTTP_PORT": "9001", "CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": AUD}
    cfg = remote.http_config(args, env=env)
    assert (cfg.port, cfg.access) == (9001, POLICY)
    assert cli.build_parser().parse_args([]).cmd is None


def test_unsafe_bind_is_refused_before_serving(tmp_path: object) -> None:
    exe = shutil.which("mcp-server-steam-backlog", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("entry point not installed (pip install -e .)")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CF_ACCESS_", "MCP_"))}
    out = subprocess.run(
        [exe, "serve", "--http", "--bind", "0.0.0.0"], env=env, capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 2
    assert "Refusing to serve on 0.0.0.0 without Cloudflare Access" in out.stderr
