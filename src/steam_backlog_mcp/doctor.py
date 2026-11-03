"""`mcp-server-steam-backlog doctor`: check everything between here and the backlog, layer by layer.

  config    - BACKLOG_URL parses; dry run on or off
  reach     - backlog_server.py answers /healthz
  api       - its API version (X-Backlog-Api; absent = 1) and which contract features it has:
              library ETags, conditional PUTs and previous values, the export routes
  library   - an export exists, its schema and age, how many games
  access    - when CF_ACCESS_* is set: both halves, and MCP_PUBLIC_HOSTS
  port      - the HTTP port is free to bind (or already this server)

Read-only: no writes, no export started. Settings it doesn't know (phase 2's MQTT_*) are listed
by name only; values never print.
"""

from __future__ import annotations

import json
import os
import platform
import socket
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version

from .client import BacklogClient, BacklogError, export_age_days
from .config import Settings
from .logsafe import mask_url, redact

STALE_DAYS = 14


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

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


def versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for pkg in ("mcp-server-steam-backlog", "mcp", "httpx"):
        try:
            out[pkg] = version(pkg)
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


async def run_doctor(settings: Settings, env: dict[str, str] | None = None) -> Report:
    e = dict(os.environ if env is None else env)
    report = Report(versions(), settings.url, settings.dry_run)
    report.checks.append(
        Check("config", True, f"BACKLOG_URL {settings.url}" + (", dry run" if settings.dry_run else ""))
    )
    if settings.extra:
        report.checks.append(
            Check("config", True, "other settings present: " + ", ".join(sorted(settings.extra)), warn=True)
        )
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
