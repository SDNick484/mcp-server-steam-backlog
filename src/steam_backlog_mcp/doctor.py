"""`mcp-server-steam-backlog doctor`: check everything between here and the backlog, layer by layer.

  config    - BACKLOG_URL parses; dry run on or off
  reach     - backlog_server.py answers /healthz
  api       - its API version (X-Backlog-Api; absent = 1) and which contract features it has:
              library ETags, conditional PUTs and previous values, the export routes
  library   - an export exists, its schema and age, how many games
  access    - when CF_ACCESS_* is set: both halves, and MCP_PUBLIC_HOSTS
  port      - the HTTP port is free to bind (or already this server)
  mqtt      - with MQTT_HOST set (now playing): the broker login works, the subscription to the Deck
              bridge's topics is allowed, and a retained state arrives within 5 seconds
  acl       - only with --probe-acl: whether this login can publish (it shouldn't)

Read-only: no writes, no export started, nothing published, except --probe-acl's one empty,
non-retained test message under <base>/acl-probe/. Settings it doesn't know are listed by name only;
values never print.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version

import aiomqtt

from . import _build, deck
from .client import BacklogClient, BacklogError, export_age_days
from .config import MqttSettings, Settings
from .logsafe import mask_host_port, mask_url, redact

STALE_DAYS = 14
RETAINED_WAIT_S = 5.0  # how long a retained state gets to arrive after subscribing
PROBE_WAIT_S = 2.0  # how long the ACL probe waits for its own message to come back


@dataclass
class Check:
    layer: str
    ok: bool
    detail: str
    hint: str = ""
    warn: bool = False  # ok, but worth a look


@dataclass
class Report:
    versions: dict[str, str]
    url: str
    dry_run: bool
    api: int | None = None
    features: dict[str, bool] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    build: str = ""  # the commit this install was built from, or "unknown"

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


def versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for pkg in ("mcp-server-steam-backlog", "mcp", "httpx"):
        try:
            out[pkg] = version(pkg)
            if pkg == "mcp-server-steam-backlog":
                out[pkg] += f" ({_build.commit()})"
        except PackageNotFoundError:
            out[pkg] = "not installed"
    return out


def _port_check(env: dict[str, str]) -> Check:
    try:
        port = int(env.get("MCP_HTTP_PORT", "8486"))
    except ValueError:
        return Check("port", False, f"MCP_HTTP_PORT {env.get('MCP_HTTP_PORT')!r} isn't a number")
    bind = env.get("MCP_HTTP_BIND", "127.0.0.1")
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((bind, port))
    except OSError:
        return Check(
            "port",
            True,
            f"{port} is in use (fine if it's this server's service)",
            hint=f"If the service won't start, find what holds the port: netstat -tlnp | grep {port}",
            warn=True,
        )
    finally:
        s.close()
    return Check("port", True, f"{port} is free")


def _access_check(env: dict[str, str]) -> Check | None:
    team, aud = env.get("CF_ACCESS_TEAM_DOMAIN"), env.get("CF_ACCESS_AUD")
    if not team and not aud:
        return None
    if not (team and aud):
        return Check(
            "access", False, "only one of CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD is set",
            hint="Set both in /etc/mcp-server-steam-backlog/env, or neither.",
        )  # fmt: skip
    hosts = env.get("MCP_PUBLIC_HOSTS")
    if not hosts:
        return Check(
            "access", True, "Access is configured, but MCP_PUBLIC_HOSTS is empty",
            hint="Set MCP_PUBLIC_HOSTS to the public hostname, or tunneled requests get 421.", warn=True,
        )  # fmt: skip
    return Check("access", True, "Cloudflare Access settings complete")


async def _collect(client: aiomqtt.Client, timeout: float, done: Callable[[str, bytes], bool]) -> None:
    """Feed messages to `done(topic, payload)` until it returns True or `timeout` passes."""
    try:
        async with asyncio.timeout(timeout):
            async for message in client.messages:
                payload = message.payload
                if isinstance(payload, str):
                    data = payload.encode()
                elif isinstance(payload, bytes | bytearray):
                    data = bytes(payload)
                else:  # aiomqtt also types numbers and None; the bridge never sends them
                    data = b""
                if done(message.topic.value, data):
                    return
    except TimeoutError:
        return


async def mqtt_checks(s: MqttSettings, *, probe_acl: bool) -> list[Check]:
    """Login, subscription, a retained state; with probe_acl, one test publish. Never prints values."""
    checks: list[Check] = []
    w = deck.DeckWatcher(s)  # only for its parsing and per-Deck cache, not its connection loop
    entered = False
    try:
        async with deck.make_client(s) as client:
            entered = True
            checks.append(Check("mqtt", True, "logged in to the broker" + (" over TLS" if s.tls else "")))
            for topic in (s.state_topic, s.availability_topic):
                if deck.refused(await client.subscribe(topic, qos=0)):
                    checks.append(
                        Check(
                            "mqtt", False, f"the broker refused the subscription to {topic}",
                            hint=f"Give this login read access to the bridge's topics, e.g. `topic read {s.base}/#` "
                            "in the broker's ACL for it.",
                        )
                    )  # fmt: skip
                    return checks
            checks.append(Check("mqtt", True, f"allowed to read {s.state_topic} and {s.availability_topic}"))
            w.connected_now()
            first: list[float] = []
            loop = asyncio.get_running_loop()

            def until_settled(topic: str, payload: bytes) -> bool:
                w.handle(topic, payload)
                if any(d.state for d in w.snapshot()) and not first:
                    first.append(loop.time())
                return bool(first) and loop.time() - first[0] >= deck.SETTLE_S

            await _collect(client, RETAINED_WAIT_S, until_settled)
            decks = [d for d in w.snapshot() if d.state]
            if decks:
                avail = {True: "online", False: "offline", None: "availability unknown"}
                desc = ", ".join(
                    f"{d.device} ({avail[d.online]}, {'playing' if d.state and d.state.playing else 'idle'})"
                    for d in decks
                )
                checks.append(
                    Check(
                        "mqtt", True, f"retained state from {len(decks)} Deck{'s' if len(decks) != 1 else ''}: {desc}"
                    )
                )
            else:
                problems = [d.problem for d in w.snapshot() if d.problem]
                checks.append(
                    Check(
                        "mqtt", False, f"no retained state on {s.state_topic} within {RETAINED_WAIT_S:g}s"
                        + (f" ({problems[0]})" if problems else ""),
                        hint="Is the Steam Deck bridge installed and pointed at this broker, and has it connected "
                        "since the broker last started? DECK_STATE_TOPIC must match its topic.",
                    )
                )  # fmt: skip
            if probe_acl:
                checks.append(await _probe(client, s))
    except aiomqtt.MqttError as exc:
        rc = getattr(exc, "rc", None)
        code = getattr(rc, "value", rc)
        if not entered and isinstance(exc, aiomqtt.MqttCodeError) and code in deck.REFUSED_LOGIN:
            checks.append(
                Check(
                    "mqtt", False, "the broker refused the login",
                    hint="Check MQTT_USER and MQTT_PASSWORD in /etc/mcp-server-steam-backlog/env (set the password "
                    "there with vim).",
                )
            )  # fmt: skip
        elif not entered:
            checks.append(
                Check(
                    "mqtt", False, f"can't reach the broker at {mask_host_port(f'{s.host}:{s.port}')}",
                    hint="Check MQTT_HOST, MQTT_PORT and MQTT_TLS, and that the broker is running.",
                )
            )  # fmt: skip
        elif probe_acl and checks and checks[-1].layer == "mqtt":
            # The connection ended during the probe: some brokers disconnect a client that publishes
            # where it may not. That's a refusal.
            checks.append(Check("acl", True, "the broker closed the connection on the test publish: read-only"))
        else:
            checks.append(Check("mqtt", False, "the broker closed the connection"))
    if probe_acl and not entered:
        checks.append(Check("acl", True, "not probed: no connection", warn=True))
    return checks


async def _probe(client: aiomqtt.Client, s: MqttSettings) -> Check:
    """Publish one empty, non-retained message under <base>/acl-probe/ and see whether it comes back.

    An MQTT 3.1.1 broker can't tell a client that a PUBLISH was refused: Mosquitto acknowledges it and
    drops it. So the probe subscribes to its own topic first; if the message is delivered back, the
    broker accepted it. Empty and not retained: nothing is stored and nobody else listens there.
    """
    topic = f"{s.base}/acl-probe/{secrets.token_hex(4)}"
    if deck.refused(await client.subscribe(topic, qos=0)):
        return Check(
            "acl", True, f"can't tell: this login may not read {topic}, so the probe can't see its own message",
            warn=True,
        )  # fmt: skip
    await client.publish(topic, b"", qos=1, retain=False)
    seen: list[bool] = []

    def back(t: str, _payload: bytes) -> bool:
        if t == topic:
            seen.append(True)
        return bool(seen)

    await _collect(client, PROBE_WAIT_S, back)
    if seen:
        return Check(
            "acl", True, "this login can publish; the ACL is not read-only",
            hint=f"Limit it in the broker's ACL to `topic read {s.base}/#`. The server never publishes, but a "
            "read-only login is safer if its password leaks.",
            warn=True,
        )  # fmt: skip
    return Check("acl", True, "the broker dropped this login's test message: read-only")


async def run_doctor(settings: Settings, env: dict[str, str] | None = None, *, probe_acl: bool = False) -> Report:
    e = dict(os.environ if env is None else env)
    report = Report(versions(), settings.url, settings.dry_run, build=_build.commit())
    report.checks.append(
        Check("config", True, f"BACKLOG_URL {settings.url}" + (", dry run" if settings.dry_run else ""))
    )
    if settings.extra:
        report.checks.append(
            Check("config", True, "other settings present: " + ", ".join(sorted(settings.extra)), warn=True)
        )
    for problem in settings.mqtt_problems:
        report.checks.append(Check("mqtt", True, problem, warn=True))
    client = BacklogClient(settings)
    try:
        try:
            healthy = await client.health()
        except BacklogError as exc:
            report.checks.append(Check("reach", False, str(exc)))
            return report
        if not healthy:
            report.checks.append(
                Check("reach", False, "/healthz didn't answer ok", hint="Is BACKLOG_URL the backlog server?")
            )
            return report
        report.checks.append(Check("reach", True, "backlog server answers /healthz"))

        f = await client.features()
        report.api = f.api
        report.features = {
            "library_etag": f.library_etag,
            "conditional_puts": f.conditional_puts,
            "previous_values": f.previous_values,
            "export": f.export,
        }
        missing = [k for k, v in report.features.items() if not v]
        if f.api >= 2 and not missing:
            report.checks.append(Check("api", True, f"API {f.api}: every contract feature present"))
        else:
            report.checks.append(
                Check(
                    "api", True, f"API {f.api}: missing {', '.join(missing)}",
                    hint=f.detail.get("export")
                    or "Update steam-backlog-triage (git pull && sh install/alpine.sh) for caching, safe reorders "
                    "and exports. Everything else works.",
                    warn=True,
                )
            )  # fmt: skip

        try:
            lib = await client.library()
        except BacklogError as exc:
            report.checks.append(Check("library", False, str(exc)))
        else:
            age = export_age_days(lib.exported_at)
            stale = age is not None and age > STALE_DAYS
            report.checks.append(
                Check(
                    "library", True,
                    f"{len(lib.games)} games, schema {lib.schema}, exported {lib.exported_at or 'at an unknown time'}"
                    + (f" ({age:g} days ago)" if age is not None else ""),
                    hint="The export is over two weeks old: is the weekly cron job running?" if stale else "",
                    warn=stale,
                )
            )  # fmt: skip
    finally:
        await client.aclose()
    access = _access_check(e)
    if access:
        report.checks.append(access)
    report.checks.append(_port_check(e))
    if settings.mqtt is not None:
        report.checks.extend(await mqtt_checks(settings.mqtt, probe_acl=probe_acl))
    elif probe_acl:
        report.checks.append(Check("acl", True, "MQTT_HOST isn't set: now playing is off, nothing to probe", warn=True))
    return report


def render(report: Report, redacted: bool = True) -> str:
    def r(s: str) -> str:
        return redact(s) if redacted else s

    lines = [
        "mcp-server-steam-backlog doctor",
        "  " + ", ".join(f"{k} {v}" for k, v in report.versions.items()),
        f"  backlog: {r(mask_url(report.url) if redacted else report.url)}",
        "",
    ]
    for c in report.checks:
        mark = "ok  " if c.ok and not c.warn else "warn" if c.ok else "FAIL"
        lines.append(f"[{mark}] {c.layer:<8} {r(c.detail)}")
        if c.hint:
            lines.append(f"         {r(c.hint)}")
    lines.append("")
    lines.append("All good." if report.ok else "Something needs attention (see FAIL above).")
    return "\n".join(lines)


def to_json(report: Report, redacted: bool = True) -> str:
    text = json.dumps({**asdict(report), "ok": report.ok}, indent=2)
    return redact(text) if redacted else text
