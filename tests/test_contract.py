"""docs/backlog-api-contract.md, as tests, against three servers:

  fake-v2  sim/fake_backlog.py implementing API 2 (what PR B is built against)
  fake-v1  the same fake imitating steam-backlog-triage before PR A
  real     a real backlog_server.py (BACKLOG_REF / BACKLOG_CHECKOUT; see conftest.py)

Each test reads the server's X-Backlog-Api and checks API 2 behavior on an API 2 server and
today's behavior on an API 1 server, so the same file validates PR A the day it lands:
  BACKLOG_CHECKOUT=../steam-backlog-triage pytest tests/test_contract.py -k real
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from steam_backlog_mcp.client import BacklogClient, BacklogError, Conflict
from steam_backlog_mcp.config import Settings
from steam_backlog_mcp.sim.fake_backlog import FakeBacklog

from .conftest import RealBacklog

ETAG_FORMAT = r'^"[0-9a-f]{32}"$'


@dataclass
class Target:
    name: str
    url: str
    fake: FakeBacklog | None = None
    real: RealBacklog | None = None

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return httpx.get(self.url + path, timeout=5, **kw)

    def head(self, path: str, **kw: Any) -> httpx.Response:
        return httpx.head(self.url + path, timeout=5, **kw)

    def put(self, path: str, body: Any, **kw: Any) -> httpx.Response:
        return httpx.put(self.url + path, content=json.dumps(body), timeout=5, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return httpx.post(self.url + path, timeout=5, **kw)

    @property
    def api(self) -> int:
        v = self.get("/healthz").headers.get("x-backlog-api")
        return int(v) if v else 1

    @property
    def export_available(self) -> bool:
        if self.real is not None:
            return self.real.api_option_export
        return True


@pytest.fixture(params=["fake-v2", "fake-v1", "real"])
def target(request: pytest.FixtureRequest) -> Iterator[Target]:
    if request.param == "real":
        real: RealBacklog = request.getfixturevalue("real_backlog")
        yield Target("real", real.url, real=real)
        return
    with FakeBacklog(api=2 if request.param == "fake-v2" else 1) as f:
        f.export_step_s = 0.05
        yield Target(request.param, f.url, fake=f)


# --------------------------------------------------------------------------- §1 headers
def test_every_response_carries_the_server_header(target: Target) -> None:
    for r in (target.get("/healthz"), target.get("/api/triage"), target.get("/nope")):
        assert r.headers.get("x-backlog-server") == "1"
    api = target.get("/nope").headers.get("x-backlog-api")
    assert api in (None, "2")


# --------------------------------------------------------------------------- §2 ETags on GET
@pytest.mark.parametrize("path", ["/api/library", "/api/triage", "/api/notes", "/api/order"])
def test_get_etag_and_304(target: Target, path: str) -> None:
    r = target.get(path)
    assert r.status_code == 200
    tag = r.headers.get("etag")
    if target.api < 2:
        assert tag is None
        # API 1 ignores If-None-Match: a full 200 every time.
        assert target.get(path, headers={"If-None-Match": '"x"'}).status_code == 200
        return
    assert tag is not None
    import re

    assert re.match(ETAG_FORMAT, tag)
    assert r.headers.get("cache-control") == "no-store"
    for inm in (tag, f"W/{tag}", f'"nope", {tag}', "*"):
        n = target.get(path, headers={"If-None-Match": inm})
        assert n.status_code == 304, inm
        assert n.content == b""
        assert n.headers.get("etag") == tag
        assert n.headers.get("x-backlog-api") == "2"
    assert target.get(path, headers={"If-None-Match": '"nope"'}).status_code == 200
    assert target.get(path, headers={"If-None-Match": "garbage"}).status_code == 200
    h = target.head(path)
    assert h.status_code == 200 and h.headers.get("etag") == tag and h.content == b""


def test_etag_is_sha256_of_the_body(target: Target) -> None:
    if target.api < 2:
        pytest.skip("API 1")
    import hashlib

    for path in ("/api/library", "/api/triage", "/api/order"):
        r = target.get(path)
        assert r.headers["etag"] == '"' + hashlib.sha256(r.content).hexdigest()[:32] + '"'


def test_etag_changes_when_the_resource_does(target: Target) -> None:
    if target.api < 2:
        pytest.skip("API 1")
    before = target.get("/api/triage").headers["etag"]
    assert target.put("/api/triage", {"appid": 620, "lane": "someday"}).status_code == 200
    after = target.get("/api/triage").headers["etag"]
    assert before != after
    assert target.get("/api/triage", headers={"If-None-Match": before}).status_code == 200


def test_library_404_before_the_first_export(target: Target) -> None:
    if target.fake is not None:
        target.fake.library = None
    elif target.real is not None:
        (target.real.data / "steam_backlog.json").unlink()
    r = target.get("/api/library")
    assert r.status_code == 404 and r.json() == {"error": "no export yet"}
    assert r.headers.get("etag") is None


# --------------------------------------------------------------------------- §3 PUTs
def test_put_triage_returns_previous(target: Target) -> None:
    r1 = target.put("/api/triage", {"appid": 620, "lane": "next"})
    r2 = target.put("/api/triage", {"appid": 620, "lane": "done"})
    r3 = target.put("/api/triage", {"appid": 620, "lane": ""})
    if target.api < 2:
        assert [r.json() for r in (r1, r2, r3)] == [{"ok": True}] * 3
        return
    assert r1.json() == {"ok": True, "appid": 620, "lane": "next", "previous": ""}
    assert r2.json() == {"ok": True, "appid": 620, "lane": "done", "previous": "next"}
    assert r3.json() == {"ok": True, "appid": 620, "lane": "", "previous": "done"}
    assert r3.headers["etag"] == target.get("/api/triage").headers["etag"]


def test_put_notes_returns_previous_and_stored_text(target: Target) -> None:
    r1 = target.put("/api/notes", {"appid": 620, "note": "  co-op with a friend  "})
    r2 = target.put("/api/notes", {"appid": 620, "note": ""})
    if target.api < 2:
        assert r1.json() == {"ok": True} and r2.json() == {"ok": True}
        return
    assert r1.json() == {"ok": True, "appid": 620, "note": "co-op with a friend", "previous": ""}
    assert r2.json() == {"ok": True, "appid": 620, "note": "", "previous": "co-op with a friend"}


def test_put_order_if_match(target: Target) -> None:
    assert target.put("/api/order", {"order": [620, 400]}).status_code == 200
    if target.api < 2:
        # API 1 ignores If-Match entirely: even a wrong tag writes.
        r = target.put("/api/order", {"order": [400, 620, 400]}, headers={"If-Match": '"stale"'})
        assert r.status_code == 200 and r.json() == {"ok": True, "count": 2}
        assert target.get("/api/order").json() == [400, 620]
        return
    tag = target.get("/api/order").headers["etag"]
    ok = target.put("/api/order", {"order": [400, 620, 400]}, headers={"If-Match": tag})
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "count": 2, "order": [400, 620], "previous": [620, 400]}
    assert ok.headers["etag"] != tag
    stale = target.put("/api/order", {"order": [620]}, headers={"If-Match": tag})
    assert stale.status_code == 412
    current = target.get("/api/order").headers["etag"]
    assert stale.json() == {"error": "precondition failed", "etag": current}
    assert stale.headers["etag"] == current
    assert target.get("/api/order").json() == [400, 620]  # nothing written
    star = target.put("/api/order", {"order": [620]}, headers={"If-Match": "*"})
    assert star.status_code == 200


def test_if_match_on_triage_and_notes(target: Target) -> None:
    if target.api < 2:
        pytest.skip("API 1")
    tag = target.get("/api/triage").headers["etag"]
    assert target.put("/api/triage", {"appid": 1, "lane": "skip"}, headers={"If-Match": tag}).status_code == 200
    assert target.put("/api/triage", {"appid": 2, "lane": "skip"}, headers={"If-Match": tag}).status_code == 412
    ntag = target.get("/api/notes").headers["etag"]
    assert target.put("/api/notes", {"appid": 1, "note": "x"}, headers={"If-Match": ntag}).status_code == 200


def test_validation_comes_before_the_precondition(target: Target) -> None:
    if target.api < 2:
        pytest.skip("API 1")
    r = target.put("/api/notes", {"appid": 620, "note": "x" * 2001}, headers={"If-Match": '"' + "f" * 32 + '"'})
    assert r.status_code == 400
    assert r.json() == {"error": "notes are limited to 2000 characters"}


def test_validation_tightening(target: Target) -> None:
    if target.api < 2:
        pytest.skip("API 1 crashes on a non-object body (contract §3.3)")
    r = target.put("/api/triage", [1])
    assert (r.status_code, r.json()) == (400, {"error": "expected a JSON object"})
    r = target.put("/api/triage", {"appid": True, "lane": "next"})
    assert r.status_code == 400
    assert "True" not in target.get("/api/triage").json()


# --------------------------------------------------------------------------- §4 export
STATUS_KEYS = {
    "state", "origin", "id", "startedAt", "finishedAt", "elapsedSeconds", "phase", "progress", "exitCode",
    "reason", "lines", "libraryEtag", "libraryChanged", "exportedAt",
}  # fmt: skip


def test_export_routes_on_api_1(target: Target) -> None:
    if target.api >= 2:
        pytest.skip("API 2")
    assert target.post("/api/export", json={}).status_code == 501
    r = target.get("/api/export/status")
    assert (r.status_code, r.json()) == (404, {"error": "not found"})


def test_export_lifecycle(target: Target) -> None:
    if target.api < 2:
        pytest.skip("API 1")
    if not target.export_available:
        pytest.skip("this checkout has no --export-cmd")
    idle = target.get("/api/export/status")
    assert idle.status_code == 200 and set(idle.json()) == STATUS_KEYS
    assert idle.json()["state"] == "idle"
    tag0 = target.get("/api/library").headers["etag"]
    assert idle.json()["libraryEtag"] == tag0

    form = target.post("/api/export", content=b"a=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert (form.status_code, form.json()) == (415, {"error": "expected Content-Type: application/json"})

    started = target.post("/api/export", json={})
    assert started.status_code == 202
    assert started.headers.get("location") == "/api/export/status"
    s = started.json()
    assert set(s) == STATUS_KEYS
    assert (s["state"], s["origin"], s["finishedAt"], s["libraryChanged"]) == ("running", "server", None, None)
    import re

    assert re.fullmatch(r"\d{8}T\d{6}Z", s["id"])

    again = target.post("/api/export", json={})
    assert again.status_code == 409
    body = again.json()
    assert body.pop("error") == "export already running"
    assert set(body) == STATUS_KEYS and body["state"] == "running"

    deadline = time.monotonic() + 20
    while True:
        st = target.get("/api/export/status").json()
        if st["state"] != "running":
            break
        assert time.monotonic() < deadline, st
        time.sleep(0.05)
    assert st["state"] == "succeeded" and st["exitCode"] == 0 and st["reason"] is None
    assert st["libraryChanged"] is True
    assert st["libraryEtag"] == target.get("/api/library").headers["etag"] != tag0
    assert st["phase"] in ("proton", "writing", "hltb")
    assert st["finishedAt"] and isinstance(st["elapsedSeconds"], int)
    assert 0 < len(st["lines"]) <= 20
    text = "\n".join(st["lines"])
    # §4.8 redaction: no SteamID, no data path, the profile line masked.
    assert "7656119XXXXXXXXXX" not in text or "<profile>" in text
    assert "<profile>: 49 games" in text
    assert "/exports/" not in text and ("<data>/steam_backlog.json" in text or "Wrote" not in text)


def test_export_failure_reasons(target: Target) -> None:
    if target.fake is None or target.api < 2:
        pytest.skip("drives the fake's exit code")
    target.fake.export_exit = 78
    assert target.post("/api/export", json={}).status_code == 202
    deadline = time.monotonic() + 5
    while (st := target.get("/api/export/status").json())["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert (st["state"], st["exitCode"], st["reason"]) == ("failed", 78, "not_configured")


def test_external_export_counts_as_running(target: Target) -> None:
    if target.api < 2 or not target.export_available:
        pytest.skip("API 1")
    if target.fake is not None:
        target.fake.external_export = True
    elif target.real is not None:
        import fcntl

        lock = open(target.real.data / ".export.lock", "w")  # noqa: SIM115 - held for the test
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    st = target.get("/api/export/status").json()
    assert (st["state"], st["origin"], st["id"], st["lines"]) == ("running", "external", None, [])
    r = target.post("/api/export", json={})
    assert r.status_code == 409 and r.json()["origin"] == "external"


# --------------------------------------------------------------------------- the client against each server
@pytest.mark.anyio
async def test_client_library_is_cached_by_etag(target: Target) -> None:
    c = BacklogClient(Settings(url=target.url))
    try:
        a = await c.library()
        b = await c.library()
        assert len(a.games) == 49 and b is a
        if c.api == 2:
            assert a.etag is not None
            if target.fake is not None:
                gets = [r for r in target.fake.requests if r[1] == "/api/library"]
                assert len(gets) == 2 and gets[1][2].get("if-none-match") == a.etag
        else:
            assert a.etag is None
    finally:
        await c.aclose()


@pytest.mark.anyio
async def test_client_library_carries_tags(target: Target) -> None:
    """The optional "tags" field (§6.7) passes through untouched: a list, [], or no key at all."""
    raw = {g["appid"]: g for g in target.get("/api/library").json()["games"]}
    assert raw[367520]["tags"] == ["Metroidvania", "Souls-like", "Difficult", "2D", "Atmospheric"]
    assert raw[999990]["tags"] == [] and "tags" not in raw[504230]
    c = BacklogClient(Settings(url=target.url))
    try:
        games = {g.appid: g for g in (await c.library()).games}
        assert games[367520].tags[:2] == ("Metroidvania", "Souls-like") and "soulslike" in games[553420].tag_keys
        assert games[999990].tags == games[504230].tags == ()
    finally:
        await c.aclose()


@pytest.mark.anyio
async def test_client_writes_report_previous(target: Target) -> None:
    c = BacklogClient(Settings(url=target.url))
    try:
        await c.set_lane(620, "someday")
        w = await c.set_lane(620, "done")
        assert (w.previous, w.stored, w.atomic) == ("someday", "done", c.api == 2)
        n = await c.set_note(620, " hi ")
        assert (n.previous, n.stored) == ("", "hi")
        o = await c.order()
        r = await c.set_order([620], o.etag, o.value)
        assert (r.previous, r.stored) == ([], [620])
        if c.api == 2:
            with pytest.raises(Conflict):
                await c.set_order([400], o.etag, [620])  # o.etag is stale now
    finally:
        await c.aclose()


@pytest.mark.anyio
async def test_client_export_availability(target: Target) -> None:
    c = BacklogClient(Settings(url=target.url))
    try:
        status = await c.export_status()
        if c.api < 2:
            assert status is None and await c.start_export() is None
        elif target.export_available:
            assert status is not None and status["state"] == "idle"
        f = await c.features()
        assert f.api == c.api
        assert (f.library_etag, f.conditional_puts, f.previous_values) == ((c.api == 2,) * 3)
    finally:
        await c.aclose()


@pytest.mark.anyio
async def test_client_errors_are_generic(target: Target) -> None:
    c = BacklogClient(Settings(url=target.url))
    try:
        with pytest.raises(BacklogError) as exc:
            await c._put("/api/notes", {"appid": 620, "note": "x" * 2001}, None)
        assert str(exc.value) == "The backlog server refused that change as invalid."
    finally:
        await c.aclose()
