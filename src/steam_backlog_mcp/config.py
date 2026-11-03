"""Settings, from the environment.

The server talks to one thing: steam-backlog-triage's ``backlog_server.py``, over its
JSON API. That server has no authentication, so it should only be reachable on the
same machine (``http://127.0.0.1:8484``, the default) or a private network.

  BACKLOG_URL          the backlog server (default http://127.0.0.1:8484)
  BACKLOG_DRY_RUN      1: read everything, change nothing; writes report what they would do
  BACKLOG_WRITE_RATE   writes per minute across all sessions (default 60, 1..600)
  BACKLOG_DEBUG        1: debug logging
  BACKLOG_LOG_UNREDACTED  1: show LAN addresses in logs (secrets and SteamIDs stay hidden)

Phase 2 (now playing on the Deck) will add MQTT_* settings here; ``Settings.extra``
keeps any BACKLOG_* or MQTT_* value it doesn't know yet, so doctor can report it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8484"
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 10.0
DEFAULT_WRITE_RATE = 60  # per minute
KNOWN = {"BACKLOG_URL", "BACKLOG_DRY_RUN", "BACKLOG_WRITE_RATE", "BACKLOG_DEBUG", "BACKLOG_LOG_UNREDACTED"}


def flag(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def normalize_url(raw: str) -> str:
    """'127.0.0.1:8484' or 'backlog.lan' -> 'http://...' with a port (8484 if none), no trailing slash."""
    v = raw.strip().rstrip("/")
    if "://" not in v:
        v = "http://" + v
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"BACKLOG_URL {raw!r} isn't an http URL or host name")
    if parsed.port is None and parsed.scheme == "http":
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        v = f"http://{host}:8484{parsed.path}"
    return v


@dataclass(frozen=True)
class Settings:
    url: str = DEFAULT_URL
    dry_run: bool = False
    write_rate: int = DEFAULT_WRITE_RATE
    connect_timeout_s: float = CONNECT_TIMEOUT_S
    read_timeout_s: float = READ_TIMEOUT_S
    extra: Mapping[str, str] = field(default_factory=dict)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    rate = DEFAULT_WRITE_RATE
    raw_rate = e.get("BACKLOG_WRITE_RATE")
    if raw_rate:
        try:
            rate = min(max(int(raw_rate), 1), 600)
        except ValueError:
            log.warning("Ignoring BACKLOG_WRITE_RATE %r: not a whole number", raw_rate)
    extra = {k: v for k, v in e.items() if (k.startswith(("BACKLOG_", "MQTT_", "DECK_")) and k not in KNOWN)}
    return Settings(
        url=normalize_url(e.get("BACKLOG_URL") or DEFAULT_URL),
        dry_run=flag(e.get("BACKLOG_DRY_RUN", "")),
        write_rate=rate,
        extra=extra,
    )
