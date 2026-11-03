"""Async HTTP client for steam-backlog-triage's ``backlog_server.py``.

The MCP never touches the backlog's files: ``backlog_server.py`` owns them (one writer, with a
lock and atomic writes), and this is its only client here. The interface is
``docs/backlog-api-contract.md``. Two versions exist in the wild:

- **API 2** (responses carry ``X-Backlog-Api: 2``): ETags and ``If-None-Match`` -> 304,
  ``If-Match`` -> 412, PUT responses carry ``previous``, and the export routes exist.
- **API 1** (steam-backlog-triage before PR A): none of that. The client degrades:
  the library is refetched at most every LIBRARY_TTL_S seconds, previous values are read
  before writing (``atomic=False`` in the result), reorders aren't protected against a
  concurrent edit, and the export routes are "not available".

Errors the model sees are generic and actionable (``BacklogError``); the detail goes to the
log, which logsafe redacts. Nothing from the upstream body is passed through.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import domain
from .config import Settings
from .logsafe import mask_url, register_custom_pattern

log = logging.getLogger(__name__)

LIBRARY_TTL_S = 60.0  # API 1 only: how long a fetched library is reused without asking
STEAMID = re.compile(r"\b7656119\d{10}\b")
# SteamIDs never reach a log, even with --no-redact: they're personal, like a key.
register_custom_pattern(STEAMID, "7656119XXXXXXXXXX")

UNREACHABLE = (
    "The backlog service isn't responding. Check that steam-backlog-triage's server is running "
    "(on Alpine: rc-service backlog-triage status) and that BACKLOG_URL points at it."
)
TIMED_OUT = "The backlog service timed out. Try again in a few seconds (retry after 5 s)."
UPSTREAM = "The backlog service returned an error. Try again; if it keeps happening, check its log."
NO_EXPORT = (
    "There's no library export on the backlog server yet. Start one with backlog_start_export, "
    "or wait for the scheduled weekly export."
)


class BacklogError(ToolError):
    """A failure the model (and the user) can act on. Only ToolError text reaches the client."""


class Conflict(BacklogError):
    """A conditional write lost to a concurrent change (412)."""


@dataclass
class Library:
    """The parsed export. ``steamid`` and ``persona`` are dropped at parse time."""

    games: list[domain.Game]
    schema: int | None
    exported_at: str | None
    etag: str | None
    fetched_at: float

    def by_id(self) -> dict[int, domain.Game]:
        return {g.appid: g for g in self.games}


@dataclass
class Versioned:
    """A small resource (triage, notes or order) and its ETag (None on API 1)."""

    value: Any
    etag: str | None


@dataclass
class WriteResult:
    previous: Any
    stored: Any
    etag: str | None
    atomic: bool  # previous came from the server's own lock (API 2), not a separate read


@dataclass
class ExportStart:
    started: bool
    status: dict[str, Any]


@dataclass
class Features:
    api: int
    library_etag: bool = False
    conditional_puts: bool = False
    previous_values: bool = False
    export: bool = False
    detail: dict[str, str] = field(default_factory=dict)


def export_age_days(exported_at: str | None, now: datetime | None = None) -> float | None:
    """Days since the export's exportedAt (ISO 8601), or None if it's missing or unreadable."""
    if not exported_at:
        return None
    try:
        t = datetime.fromisoformat(exported_at)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return round(((now or datetime.now(UTC)) - t).total_seconds() / 86400, 1)


def parse_library(data: Any, etag: str | None) -> Library:
    if not isinstance(data, dict) or not isinstance(data.get("games"), list):
        raise BacklogError("The backlog server's library isn't an export file. Run a new export.")
    games = [domain.normalize(g, i) for i, g in enumerate(data["games"]) if isinstance(g, dict) and "appid" in g]
    schema = data.get("schema")
    exported = data.get("exportedAt")
    return Library(
        games=games,
        schema=schema if isinstance(schema, int) else None,
        exported_at=exported if isinstance(exported, str) else None,
        etag=etag,
        fetched_at=time.monotonic(),
    )


def _triage_map(raw: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and k.isdigit() and v in domain.LANES:
                out[int(k)] = v
    return out


def _notes_map(raw: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and k.isdigit() and isinstance(v, str) and v:
                out[int(k)] = v
    return out


def _order_list(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, int) and not isinstance(a, bool)]


class BacklogClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.url,
            timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
            headers={"User-Agent": "mcp-server-steam-backlog", "Accept": "application/json"},
            transport=transport,
        )
        self.api: int | None = None  # learned from X-Backlog-Api on every response
        self._library: Library | None = None
        self._library_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ transport
    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.TimeoutException as exc:
            log.warning("%s %s timed out (%s)", method, path, type(exc).__name__)
            raise BacklogError(TIMED_OUT) from exc
        except httpx.HTTPError as exc:
            log.warning("%s %s%s failed: %s", method, mask_url(self.settings.url), path, exc)
            raise BacklogError(UNREACHABLE) from exc
        api = resp.headers.get("x-backlog-api")
        self.api = int(api) if api and api.isdigit() else 1
        return resp

    def _fail(self, resp: httpx.Response, what: str) -> BacklogError:
        log.warning("%s: HTTP %s %s", what, resp.status_code, resp.text[:300])
        return BacklogError(UPSTREAM)

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("unparseable JSON from %s: %s", resp.request.url.path, exc)
            raise BacklogError(UPSTREAM) from exc

    # ------------------------------------------------------------------ reads
    async def health(self) -> bool:
        resp = await self._request("GET", "/healthz")
        return resp.status_code == 200

    async def library(self) -> Library:
        """The parsed library: revalidated with If-None-Match on API 2 (a 304 costs no parse),
        reused for LIBRARY_TTL_S on API 1."""
        async with self._library_lock:
            cached = self._library
            fresh = cached is not None and time.monotonic() - cached.fetched_at < LIBRARY_TTL_S
            if cached is not None and cached.etag is None and self.api == 1 and fresh:
                return cached
            headers = {"If-None-Match": cached.etag} if cached is not None and cached.etag else {}
            resp = await self._request("GET", "/api/library", headers=headers)
            if resp.status_code == 304 and cached is not None:
                cached.fetched_at = time.monotonic()
                return cached
            if resp.status_code == 404:
                self._library = None
                raise BacklogError(NO_EXPORT)
            if resp.status_code != 200:
                raise self._fail(resp, "GET /api/library")
            lib = parse_library(self._json(resp), resp.headers.get("etag"))
            self._library = lib
            log.info("library loaded: %d games (etag %s)", len(lib.games), lib.etag or "none, API 1")
            return lib

    def forget_library(self) -> None:
        self._library = None

    async def _get_small(self, path: str) -> tuple[Any, str | None]:
        resp = await self._request("GET", path)
        if resp.status_code != 200:
            raise self._fail(resp, f"GET {path}")
        return self._json(resp), resp.headers.get("etag")

    async def triage(self) -> Versioned:
        raw, etag = await self._get_small("/api/triage")
        return Versioned(_triage_map(raw), etag)

    async def notes(self) -> Versioned:
        raw, etag = await self._get_small("/api/notes")
        return Versioned(_notes_map(raw), etag)

    async def order(self) -> Versioned:
        raw, etag = await self._get_small("/api/order")
        return Versioned(_order_list(raw), etag)

    # ------------------------------------------------------------------ writes
    async def _put(self, path: str, body: Mapping[str, Any], if_match: str | None) -> httpx.Response:
        headers = {"If-Match": if_match} if if_match else {}
        resp = await self._request("PUT", path, json=dict(body), headers=headers)
        if resp.status_code == 412:
            raise Conflict("Someone changed this on the dashboard at the same moment. Read it again and retry.")
        if resp.status_code == 400:
            log.warning("PUT %s refused: %s", path, resp.text[:300])
            raise BacklogError("The backlog server refused that change as invalid.")
        if resp.status_code != 200:
            raise self._fail(resp, f"PUT {path}")
        return resp

    async def set_lane(self, appid: int, lane: str) -> WriteResult:
        """lane '' clears. On API 1 the previous lane is read first (not atomic)."""
        before: str | None = None
        if self.api != 2:
            before = (await self.triage()).value.get(appid, "")
        resp = await self._put("/api/triage", {"appid": appid, "lane": lane}, None)
        body = self._json(resp)
        if isinstance(body, dict) and "previous" in body:
            return WriteResult(
                str(body.get("previous") or ""), str(body.get("lane", lane)), resp.headers.get("etag"), True
            )
        return WriteResult(before or "", lane, None, False)

    async def set_note(self, appid: int, note: str) -> WriteResult:
        before: str | None = None
        if self.api != 2:
            before = (await self.notes()).value.get(appid, "")
        resp = await self._put("/api/notes", {"appid": appid, "note": note}, None)
        body = self._json(resp)
        if isinstance(body, dict) and "previous" in body:
            return WriteResult(
                str(body.get("previous") or ""), str(body.get("note", note.strip())), resp.headers.get("etag"), True
            )
        return WriteResult(before or "", note.strip(), None, False)

    async def set_order(self, order: list[int], if_match: str | None, previous: list[int]) -> WriteResult:
        resp = await self._put("/api/order", {"order": order}, if_match)
        body = self._json(resp)
        if isinstance(body, dict) and "previous" in body:
            return WriteResult(
                _order_list(body.get("previous")),
                _order_list(body.get("order", order)),
                resp.headers.get("etag"),
                True,
            )
        return WriteResult(previous, order, None, False)

    # ------------------------------------------------------------------ export
    async def export_status(self) -> dict[str, Any] | None:
        """The status object, or None when the server has no export routes (API 1, or not configured)."""
        resp = await self._request("GET", "/api/export/status")
        if resp.status_code in (404, 405, 501):
            return None
        if resp.status_code != 200:
            raise self._fail(resp, "GET /api/export/status")
        body = self._json(resp)
        if not isinstance(body, dict):
            raise BacklogError(UPSTREAM)
        return body

    async def start_export(self) -> ExportStart | None:
        resp = await self._request("POST", "/api/export", json={})
        if resp.status_code in (404, 405, 501):
            return None
        if resp.status_code in (202, 409):
            body = self._json(resp)
            if not isinstance(body, dict):
                raise BacklogError(UPSTREAM)
            body.pop("error", None)
            if resp.status_code == 202:
                self.forget_library()
            return ExportStart(started=resp.status_code == 202, status=body)
        raise self._fail(resp, "POST /api/export")

    # ------------------------------------------------------------------ doctor
    async def features(self) -> Features:
        """Which parts of the contract this backlog server has, by asking (read-only)."""
        resp = await self._request("HEAD", "/api/library")
        api = self.api or 1
        f = Features(api=api)
        f.library_etag = bool(resp.headers.get("etag"))
        triage = await self._request("HEAD", "/api/triage")
        f.conditional_puts = api >= 2 and bool(triage.headers.get("etag"))
        f.previous_values = api >= 2
        status = await self._request("GET", "/api/export/status")
        f.export = status.status_code == 200
        if status.status_code == 501:
            f.detail["export"] = "the server has no export command configured"
        return f
