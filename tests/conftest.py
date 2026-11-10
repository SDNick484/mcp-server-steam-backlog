"""Shared fixtures.

Most tests talk to the wire-level fake (sim/fake_backlog.py): real HTTP on a free port. The
contract tests also run against a real backlog_server.py (``real_backlog``):

  BACKLOG_CHECKOUT=/path/to/steam-backlog-triage   use that checkout as it is (e.g. PR A's branch)
  BACKLOG_REF=<branch, tag or sha>                 clone github.com/SDNick484/steam-backlog-triage at it
                                                   (default: 4d0b53c, triage main after its CLAUDE.md
                                                   and pin PR; API 2)
  BACKLOG_REQUIRE_REAL=1                           fail instead of skipping when it can't be had (CI)
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from steam_backlog_mcp import server
from steam_backlog_mcp.sim.fake_backlog import FIXTURE, FakeBacklog

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
BACKLOG_REPO = "https://github.com/SDNick484/steam-backlog-triage.git"
# steam-backlog-triage's main after PR #12 (CLAUDE.md and the pin); keep in step with BACKLOG_REF in
# .github/workflows/ci.yml. Bump it when the contract changes.
DEFAULT_BACKLOG_REF = "4d0b53c3c11d88e6afb5b513572d32d78ca9f24c"


# Async tests use anyio's pytest plugin: the MCP SDK's in-process Client needs a fixture's setup
# and teardown in the same task, which anyio guarantees.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the user's real settings."""
    for var in list(os.environ):
        if var.startswith(("BACKLOG_", "MCP_", "CF_ACCESS_", "MQTT_")) and var not in (
            "BACKLOG_REF",
            "BACKLOG_CHECKOUT",
            "BACKLOG_REQUIRE_REAL",
        ):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "client_factory", server.BacklogClient)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeBacklog]:
    with FakeBacklog() as f:
        monkeypatch.setenv("BACKLOG_URL", f.url)
        yield f


@pytest.fixture
def fake_v1(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeBacklog]:
    with FakeBacklog(api=1) as f:
        monkeypatch.setenv("BACKLOG_URL", f.url)
        yield f


@pytest.fixture
def library() -> dict[str, object]:
    data: dict[str, object] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


# --------------------------------------------------------------------------- the real server
@dataclass
class RealBacklog:
    url: str
    data: Path
    api_option_export: bool  # the checkout's server accepts --export-cmd (PR A or later)
    proc: subprocess.Popen[bytes]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _checkout(cache: Path) -> Path | None:
    local = os.environ.get("BACKLOG_CHECKOUT")
    if local:
        return Path(local).expanduser().resolve()
    ref = os.environ.get("BACKLOG_REF", DEFAULT_BACKLOG_REF)
    dest = cache / f"steam-backlog-triage-{ref.replace('/', '_')}"
    if (dest / "backlog_server.py").exists():
        return dest
    try:
        subprocess.run(["git", "init", "-q", str(dest)], check=True, timeout=30)
        subprocess.run(
            ["git", "-C", str(dest), "fetch", "-q", "--depth", "1", BACKLOG_REPO, ref], check=True, timeout=120
        )
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"], check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(dest, ignore_errors=True)
        return None
    return dest


@pytest.fixture(scope="session")
def real_checkout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    co = _checkout(tmp_path_factory.mktemp("checkouts"))
    if co is None or not (co / "backlog_server.py").exists():
        msg = f"no steam-backlog-triage checkout (BACKLOG_REF={os.environ.get('BACKLOG_REF', DEFAULT_BACKLOG_REF)})"
        if os.environ.get("BACKLOG_REQUIRE_REAL") == "1":
            pytest.fail(msg)
        pytest.skip(msg)
    return co


@pytest.fixture
def real_backlog(real_checkout: Path, tmp_path: Path) -> Iterator[RealBacklog]:
    data = tmp_path / "exports"
    data.mkdir()
    shutil.copy(FIXTURE, data / "steam_backlog.json")
    help_text = subprocess.run(
        [sys.executable, str(real_checkout / "backlog_server.py"), "--help"], capture_output=True, text=True, timeout=30
    ).stdout
    has_export = "--export-cmd" in help_text
    port = _free_port()
    cmd = [sys.executable, str(real_checkout / "backlog_server.py"), "--port", str(port), "--bind", "127.0.0.1"]
    cmd += ["--data", str(data)]
    if has_export:
        cmd += ["--export-cmd", str(FIXTURES / "fake_export.sh")]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while True:
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline or proc.poll() is not None:
            proc.kill()
            pytest.fail("the real backlog_server.py didn't start")
        time.sleep(0.05)
    try:
        yield RealBacklog(url, data, has_export, proc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
