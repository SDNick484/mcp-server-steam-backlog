"""A wire-level fake of steam-backlog-triage's backlog_server.py, for tests and `simulate`.

It speaks real HTTP on a free port and implements ``docs/backlog-api-contract.md``:

- ``api=2`` (default): the contract exactly: ETags and 304, If-Match and 412, ``previous`` in
  PUT responses, ``POST /api/export`` and ``GET /api/export/status`` with a simulated export.
- ``api=1``: what steam-backlog-triage did before the contract (commit 729fa93): no
  ``X-Backlog-Api``, no ETags, plain ``{"ok": true}`` PUT responses, POST answered with the
  stdlib's 501 page, and 404 for the status route. The MCP's degraded path runs against this.

Test hooks: ``requests`` (method, path, headers) for every request; ``edit_order()`` and
``set_lane()`` change state as the dashboard would between two MCP calls; ``conflicts_left``
makes the next N conditional order PUTs answer 412; ``export_*`` attributes shape the
simulated export; ``external_export`` pretends cron holds the export lock.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LANES = {"", "next", "someday", "skip", "done"}
NOTE_MAX = 2000
ORDER_MAX = 5000
FIXTURE = Path(__file__).resolve().parent / "sample_library.json"

# What the simulated export prints, per the contract's phase table (§4.7).
EXPORT_SCRIPT = [
    ("starting", "2026-11-03 12:00:00 starting export"),
    ("library", "<profile>: {n} games"),
    ("deck", "Steam Deck compatibility: fetching 3 games..."),
    ("deck", "  3/3"),
    ("details", "Store details: fetching 2 games..."),
    ("details", "  2/2"),
    ("proton", "ProtonDB: everything cached."),
    ("hltb", "HowLongToBeat: looking up 1 games..."),
    ("hltb", "  1/1"),
    ("writing", "Wrote <data>/steam_backlog.json: {n} games, 0 never launched, 0 with HLTB times."),
]


def etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def _tags(header: str) -> list[str]:
    return [t.strip() for t in header.split(",") if t.strip()]


def _weak_eq(a: str, b: str) -> bool:
    return a.removeprefix("W/") == b.removeprefix("W/")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class FakeBacklog:
    def __init__(self, library: Mapping[str, Any] | None = None, *, api: int = 2, port: int = 0) -> None:
        self.api = api
        self.library: dict[str, Any] | None = (
            dict(library) if library is not None else json.loads(FIXTURE.read_text(encoding="utf-8"))
        )
        self.triage: dict[str, str] = {}
        self.notes: dict[str, str] = {}
        self.order: list[int] = []
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.lock = threading.Lock()
        self.conflicts_left = 0
        # Export simulation
        self.export_configured = True
        self.export_step_s = 0.02
        self.export_exit = 0  # 0 ok, 75 busy, 78 not configured, other = error
        self.export_changes_library = True
        self.external_export = False
        self._export: dict[str, Any] | None = None
        self._export_thread: threading.Thread | None = None
        self._port = port
        self._srv: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> FakeBacklog:
        self._srv = ThreadingHTTPServer(("127.0.0.1", self._port), _handler(self))
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        if self._export_thread is not None:
            self._export_thread.join(timeout=5)

    def __enter__(self) -> FakeBacklog:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._srv is not None
        return int(self._srv.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------ test hooks
    def set_lane(self, appid: int, lane: str) -> None:
        with self.lock:
            if lane:
                self.triage[str(appid)] = lane
            else:
                self.triage.pop(str(appid), None)

    def edit_order(self, order: list[int]) -> None:
        with self.lock:
            self.order = list(order)

    def writes(self) -> list[tuple[str, str, dict[str, str]]]:
        return [r for r in self.requests if r[0] in ("PUT", "POST")]

    # ------------------------------------------------------------------ bodies
    def library_bytes(self) -> bytes | None:
        if self.library is None:
            return None
        return json.dumps(self.library, ensure_ascii=False, separators=(",", ":")).encode()

    @staticmethod
    def small_bytes(value: Any) -> bytes:
        return json.dumps(value).encode()

    def resource(self, path: str) -> bytes | None:
        if path == "/api/library":
            return self.library_bytes()
        value = {"/api/triage": self.triage, "/api/notes": self.notes, "/api/order": self.order}[path]
        return self.small_bytes(value)

    # ------------------------------------------------------------------ export
    def _lib_tag(self) -> str | None:
        b = self.library_bytes()
        return etag(b) if b is not None else None

    def _exported_at(self) -> str | None:
        v = self.library.get("exportedAt") if self.library else None
        return v if isinstance(v, str) else None

    def export_status(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "state": "idle", "origin": None, "id": None, "startedAt": None, "finishedAt": None,
            "elapsedSeconds": None, "phase": None, "progress": None, "exitCode": None, "reason": None,
            "lines": [], "libraryEtag": self._lib_tag(), "libraryChanged": None, "exportedAt": self._exported_at(),
        }  # fmt: skip
        if self.external_export:
            return {**base, "state": "running", "origin": "external"}
        if self._export is None:
            return base
        e = self._export
        started = e["t0"]
        elapsed = int((e["t1"] or time.time()) - started)
        out = {**base, **{k: e[k] for k in ("state", "id", "phase", "progress", "exitCode", "reason")}}
        out.update(
            origin="server",
            startedAt=datetime.fromtimestamp(started, UTC).isoformat(timespec="seconds"),
            finishedAt=datetime.fromtimestamp(e["t1"], UTC).isoformat(timespec="seconds") if e["t1"] else None,
            elapsedSeconds=elapsed,
            lines=e["lines"][-20:],
            libraryChanged=(self._lib_tag() != e["tag0"]) if e["state"] != "running" else None,
        )
        return out

    def running(self) -> bool:
        return self.external_export or (self._export is not None and self._export["state"] == "running")

    def start_export(self) -> None:
        t0 = time.time()
        self._export = {
            "state": "running", "id": datetime.fromtimestamp(t0, UTC).strftime("%Y%m%dT%H%M%SZ"), "phase": None,
            "progress": None, "exitCode": None, "reason": None, "lines": [], "t0": t0, "t1": None,
            "tag0": self._lib_tag(),
        }  # fmt: skip
        self._export_thread = threading.Thread(target=self._run_export, daemon=True)
        self._export_thread.start()

    def _run_export(self) -> None:
        e = self._export
        assert e is not None
        n = len(self.library["games"]) if self.library else 0
        script = EXPORT_SCRIPT
        if self.export_exit == 78:
            script = [("starting", "2026-11-03 12:00:00 skipped: set STEAM_API_KEY and STEAM_PROFILE in ~/.steam-env")]
        elif self.export_exit == 75:
            script = [("starting", "2026-11-03 12:00:00 skipped: another export is running")]
        for phase, line in script:
            time.sleep(self.export_step_s)
            with self.lock:
                text = line.format(n=n)
                e["lines"].append(text)
                m = re.fullmatch(r"\s+(\d+)/(\d+)", text)
                if m:
                    e["progress"] = {"done": int(m.group(1)), "total": int(m.group(2))}
                elif e["phase"] != phase:
                    e["phase"], e["progress"] = phase, None
        with self.lock:
            if self.export_exit == 0 and self.export_changes_library and self.library is not None:
                self.library = {**self.library, "exportedAt": _now()}
            e["exitCode"] = self.export_exit
            e["state"] = "succeeded" if self.export_exit == 0 else "failed"
            e["reason"] = {0: None, 75: "busy", 78: "not_configured"}.get(self.export_exit, "error")
            e["t1"] = time.time()


def _handler(app: FakeBacklog) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "fake-backlog/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            pass

        def _record(self) -> str:
            path = self.path.split("?", 1)[0]
            app.requests.append((self.command, path, {k.lower(): v for k, v in self.headers.items()}))
            return path

        def send(self, status: int, body: bytes = b"", ctype: str = "application/json", tag: str | None = None,
                 extra: Mapping[str, str] | None = None) -> None:  # fmt: skip
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if status != 304:
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Backlog-Server", "1")
            if app.api >= 2:
                self.send_header("X-Backlog-Api", str(app.api))
                if tag:
                    self.send_header("ETag", tag)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and status != 304:
                self.wfile.write(body)

        def json(self, status: int, obj: Any, tag: str | None = None, extra: Mapping[str, str] | None = None) -> None:
            self.send(status, json.dumps(obj).encode(), tag=tag, extra=extra)

        def body(self) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 32 * 1024 * 1024:
                raise ValueError("missing or oversized body")
            return json.loads(self.rfile.read(n))

        # -------------------------------------------------------------- GET
        def do_GET(self) -> None:
            path = self._record()
            if path == "/healthz":
                return self.send(200, b"ok", "text/plain")
            if path in ("/api/library", "/api/triage", "/api/notes", "/api/order"):
                with app.lock:
                    body = app.resource(path)
                if body is None:
                    return self.json(404, {"error": "no export yet"})
                tag = etag(body)
                inm = self.headers.get("If-None-Match")
                if app.api >= 2 and inm is not None:
                    tags = _tags(inm)
                    if "*" in tags or any(_weak_eq(t, tag) for t in tags):
                        return self.send(304, tag=tag)
                return self.send(200, body, tag=tag)
            if path == "/api/export/status" and app.api >= 2:
                if not app.export_configured:
                    return self.json(501, {"error": "export not configured on this server"})
                with app.lock:
                    return self.json(200, app.export_status())
            self.json(404, {"error": "not found"})

        do_HEAD = do_GET

        # -------------------------------------------------------------- POST
        def do_POST(self) -> None:
            path = self._record()
            if app.api < 2:
                # BaseHTTPRequestHandler without do_POST: 501 with an HTML page.
                return self.send(501, b"<html><body>Unsupported method ('POST')</body></html>", "text/html")
            if path != "/api/export":
                return self.json(404, {"error": "not found"})
            if not app.export_configured:
                return self.json(501, {"error": "export not configured on this server"})
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self.json(415, {"error": "expected Content-Type: application/json"})
            with app.lock:
                if app.running():
                    return self.json(409, {"error": "export already running", **app.export_status()})
                app.start_export()
                return self.json(202, app.export_status(), extra={"Location": "/api/export/status"})

        # -------------------------------------------------------------- PUT
        def do_PUT(self) -> None:
            path = self._record()
            try:
                data = self.body()
            except (ValueError, json.JSONDecodeError) as exc:
                return self.json(400, {"error": str(exc)})
            if path not in ("/api/triage", "/api/notes", "/api/order"):
                return self.json(404, {"error": "not found"})
            if app.api < 2:
                return self._put_v1(path, data)
            if not isinstance(data, dict):
                return self.json(400, {"error": "expected a JSON object"})
            with app.lock:
                err = self._validate(path, data)
                if err:
                    return self.json(400, {"error": err})
                current = etag(app.resource(path) or b"")
                im = self.headers.get("If-Match")
                if im is not None:
                    tags = _tags(im)
                    lost = path == "/api/order" and app.conflicts_left > 0  # as if the page saved first
                    if lost:
                        app.conflicts_left -= 1
                    if lost or ("*" not in tags and current not in tags):
                        return self.json(412, {"error": "precondition failed", "etag": current}, tag=current)
                result = self._apply(path, data)
                return self.json(200, result, tag=etag(app.resource(path) or b""))

        def _validate(self, path: str, data: dict[str, Any]) -> str | None:
            appid = data.get("appid")
            is_id = isinstance(appid, int) and not isinstance(appid, bool)
            if path == "/api/triage":
                if not is_id or data.get("lane", "") not in LANES:
                    return "expected {appid: int, lane: next|someday|skip|done|''}"
            elif path == "/api/notes":
                note = data.get("note", "")
                if not is_id or not isinstance(note, str):
                    return "expected {appid: int, note: string}"
                if len(note.strip()) > NOTE_MAX:
                    return f"notes are limited to {NOTE_MAX} characters"
            else:
                order = data.get("order")
                if (
                    not isinstance(order, list)
                    or len(order) > ORDER_MAX
                    or not all(isinstance(a, int) and not isinstance(a, bool) for a in order)
                ):
                    return f"expected {{order: [app ids, up to {ORDER_MAX}]}}"
            return None

        def _apply(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
            if path == "/api/triage":
                key, lane = str(data["appid"]), data.get("lane", "")
                prev = app.triage.get(key, "")
                if lane:
                    app.triage[key] = lane
                else:
                    app.triage.pop(key, None)
                return {"ok": True, "appid": data["appid"], "lane": lane, "previous": prev}
            if path == "/api/notes":
                key, note = str(data["appid"]), data.get("note", "").strip()
                prev = app.notes.get(key, "")
                if note:
                    app.notes[key] = note
                else:
                    app.notes.pop(key, None)
                return {"ok": True, "appid": data["appid"], "note": note, "previous": prev}
            clean = list(dict.fromkeys(data["order"]))
            prev_order = app.order
            app.order = clean
            return {"ok": True, "count": len(clean), "order": clean, "previous": prev_order}

        def _put_v1(self, path: str, data: Any) -> None:
            """Today's server: no previous, no ETag, its validation quirks kept where harmless."""
            if not isinstance(data, dict):
                return self.json(400, {"error": "expected a JSON object"})
            err = self._validate(path, data)
            if err:
                return self.json(400, {"error": err})
            with app.lock:
                r = self._apply(path, data)
            if path == "/api/order":
                return self.json(200, {"ok": True, "count": r["count"]})
            return self.json(200, {"ok": True})

    return Handler
