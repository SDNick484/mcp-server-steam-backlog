"""mcp-server-steam-backlog: an MCP server for Steam Backlog Triage.

Commands:
  serve      run the MCP server: stdio (the default), or --http for a long-lived service
             behind Cloudflare Access (remote.py)
  doctor     check everything between here and the backlog server (`check` is an alias)
  simulate   run a fake backlog server with a sample library, to try things without one
  call       call one tool exactly as the model would, and print the result
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys

from . import __version__, _build, remote
from .config import flag, load_settings
from .logsafe import setup_logging


def _unredacted(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "no_redact", False)) or flag(os.environ.get("BACKLOG_LOG_UNREDACTED"))


def _setup_logging(args: argparse.Namespace) -> None:
    """stderr only (stdout belongs to the stdio transport), redacted unless --no-redact."""
    debug = getattr(args, "debug", False) or flag(os.environ.get("BACKLOG_DEBUG"))
    quiet = args.cmd in ("doctor", "check", "call", "simulate")
    level = logging.DEBUG if debug else (logging.WARNING if quiet else logging.INFO)
    setup_logging(level, redacted=not _unredacted(args))
    if not debug:
        logging.getLogger("httpx").setLevel(logging.WARNING)


async def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import render, run_doctor, to_json

    report = await run_doctor(load_settings())
    redacted = not _unredacted(args)
    print(to_json(report, redacted) if args.json else render(report, redacted))
    return 0 if report.ok else 1


async def _cmd_simulate(args: argparse.Namespace) -> int:
    from .sim.fake_backlog import FakeBacklog

    fake = FakeBacklog(api=args.api, port=args.port).start()
    n = len(fake.library["games"]) if fake.library else 0
    print(f"Simulated backlog server (API {args.api}) on {fake.url} with a {n}-game sample library.")
    print(f"\nIn another terminal:\n  export BACKLOG_URL={fake.url}")
    print("  mcp-server-steam-backlog doctor\n  mcp-server-steam-backlog call suggest_picks count=3\n\nCtrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        fake.stop()


def _parse_args(raw_args: list[str]) -> dict[str, object] | None:
    out: dict[str, object] = {}
    for pair in raw_args:
        key, sep, raw = pair.partition("=")
        if not sep:
            print(f"arguments are key=value, got {pair!r}", file=sys.stderr)
            return None
        try:
            out[key] = json.loads(raw)  # count=3 -> 3, games='["Hades"]' -> a list
        except json.JSONDecodeError:
            out[key] = raw  # game=Hades -> a string
    return out


async def _cmd_call(tool: str, raw_args: list[str]) -> int:
    """One tool call through the real MCP layer, as the model would make it."""
    from mcp import Client

    from .server import mcp

    tool_args = _parse_args(raw_args)
    if tool_args is None:
        return 2
    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        if tool == "tools":
            for t in (await c.list_tools()).tools:
                print(f"{t.name:<28} {t.title or ''}")
            return 0
        name = tool if tool in names else f"backlog_{tool}"
        if name not in names:
            for alt in (f"backlog_get_{tool}", f"backlog_{tool}_games"):
                if alt in names:
                    name = alt
                    break
        if name not in names:
            print(f"no tool {tool!r}; `call tools` lists them", file=sys.stderr)
            return 2
        result = await c.call_tool(name, tool_args)
    if result.is_error:
        print(" ".join(getattr(part, "text", "") for part in result.content) or "error", file=sys.stderr)
        return 1
    print(json.dumps(result.structured_content, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-server-steam-backlog", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__} ({_build.commit()})")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--debug", action="store_true", help="debug logging (BACKLOG_DEBUG=1)")
    common.add_argument(
        "--no-redact",
        action="store_true",
        help="show LAN addresses in logs and doctor output (BACKLOG_LOG_UNREDACTED=1); secrets stay hidden",
    )
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", parents=[common], help="run the MCP server (stdio; --http for a service)")
    serve.add_argument("--dry-run", action="store_true", help="read everything, change nothing (BACKLOG_DRY_RUN=1)")
    remote.add_http_arguments(serve, default_port=8486, default_path="/mcp/backlog")
    for name in ("doctor", "check"):
        doc = sub.add_parser(name, parents=[common], help="check everything between here and the backlog server")
        doc.add_argument("--json", action="store_true", help="the full report as JSON")
    sim = sub.add_parser("simulate", parents=[common], help="run a fake backlog server with a sample library")
    sim.add_argument("--port", type=int, default=0, help="port (default: any free one)")
    sim.add_argument("--api", type=int, choices=(1, 2), default=2, help="API version to imitate (1 = before PR A)")
    cal = sub.add_parser("call", parents=[common], help="call one tool as the model would; `call tools` lists them")
    cal.add_argument("tool")
    cal.add_argument("args", nargs="*", help="key=value (values are JSON when they parse: count=3)")
    cal.add_argument("--dry-run", action="store_true", help="change nothing")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args)
    if getattr(args, "dry_run", False):
        os.environ["BACKLOG_DRY_RUN"] = "1"  # read by load_settings() in the server's lifespan

    if args.cmd in ("doctor", "check"):
        sys.exit(asyncio.run(_cmd_doctor(args)))
    if args.cmd == "simulate":
        with contextlib.suppress(KeyboardInterrupt):
            sys.exit(asyncio.run(_cmd_simulate(args)))
        sys.exit(0)
    if args.cmd == "call":
        sys.exit(asyncio.run(_cmd_call(args.tool, args.args)))

    from .server import mcp  # imported late: doctor and simulate don't need the server

    if getattr(args, "http", False):
        try:
            remote.serve_http(mcp, remote.http_config(args))
        except remote.ConfigError as exc:
            parser.exit(2, f"mcp-server-steam-backlog: {exc}\n")
    else:
        mcp.run()
