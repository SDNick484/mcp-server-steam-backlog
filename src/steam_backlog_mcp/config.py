"""Settings, from the environment.

The server talks to one thing: steam-backlog-triage's ``backlog_server.py``, over its
JSON API. That server has no authentication, so it should only be reachable on the
same machine (``http://127.0.0.1:8484``, the default) or a private network.

  BACKLOG_URL          the backlog server (default http://127.0.0.1:8484)
  BACKLOG_DRY_RUN      1: read everything, change nothing; writes report what they would do
  BACKLOG_WRITE_RATE   writes per minute across all sessions (default 60, 1..600)
  BACKLOG_DEBUG        1: debug logging
  BACKLOG_LOG_UNREDACTED  1: show LAN addresses in logs (secrets and SteamIDs stay hidden)

Now playing on the Steam Deck (off unless MQTT_HOST is set). A read-only broker login for the
Deck bridge's retained state (steam-deck-ha-bridge, docs/state-schema.md):

  MQTT_HOST            the broker; unset = no now-playing tool and no MQTT connection
  MQTT_PORT            default 1883, or 8883 with MQTT_TLS
  MQTT_USER            the login (give it read access to deck-ha-bridge/# only)
  MQTT_PASSWORD        its password; registered with logsafe, so it never reaches a log
  MQTT_TLS             1: TLS with the system's CA certificates
  DECK_STATE_TOPIC     the bridge's state topic (default deck-ha-bridge/+/state); the availability
                       topic is the same with "availability" in place of "state"

``Settings.extra`` keeps any other BACKLOG_*, MQTT_* or DECK_* value, so doctor can list its name.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .logsafe import register_custom_pattern

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8484"
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 10.0
DEFAULT_WRITE_RATE = 60  # per minute
KNOWN = {
    "BACKLOG_URL",
    "BACKLOG_DRY_RUN",
    "BACKLOG_WRITE_RATE",
    "BACKLOG_DEBUG",
    "BACKLOG_LOG_UNREDACTED",
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_USER",
    "MQTT_PASSWORD",
    "MQTT_TLS",
    "DECK_STATE_TOPIC",
}
DEFAULT_STATE_TOPIC = "deck-ha-bridge/+/state"
# Whole levels only: letters, digits, - and _, or a single-level wildcard (+) after the first level,
# ending in /state. No # (it would match the availability topic too) and no wildcard first (the probe
# in doctor needs a fixed base level).
_TOPIC = re.compile(r"[A-Za-z0-9_-]+(/([A-Za-z0-9_-]+|\+))*/state")
MIN_SECRET_LEN = 4  # shorter passwords would redact ordinary words in every log line


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
class MqttSettings:
    host: str
    port: int = 1883
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    tls: bool = False
    state_topic: str = DEFAULT_STATE_TOPIC

    @property
    def availability_topic(self) -> str:
        return self.state_topic[: -len("state")] + "availability"

    @property
    def base(self) -> str:
        """The topic's first level: deck-ha-bridge. doctor's ACL probe publishes under it."""
        return self.state_topic.split("/", 1)[0]


def load_mqtt(e: Mapping[str, str]) -> tuple[MqttSettings | None, list[str]]:
    """The now-playing settings, or None when MQTT_HOST is unset; plus problems for doctor and the log."""
    problems: list[str] = []
    host = (e.get("MQTT_HOST") or "").strip()
    if not host:
        others = sorted(k for k in e if k.startswith("MQTT_") or k == "DECK_STATE_TOPIC")
        if others:
            problems.append(f"{', '.join(others)} set but MQTT_HOST isn't: now playing is off")
        return None, problems
    tls = flag(e.get("MQTT_TLS", ""))
    port = 8883 if tls else 1883
    raw_port = (e.get("MQTT_PORT") or "").strip()
    if raw_port:
        try:
            value = int(raw_port)
        except ValueError:
            value = 0
        if 1 <= value <= 65535:
            port = value
        else:
            problems.append(f"MQTT_PORT {raw_port!r} isn't a port number; using {port}")
    topic = (e.get("DECK_STATE_TOPIC") or "").strip() or DEFAULT_STATE_TOPIC
    if not _TOPIC.fullmatch(topic):
        problems.append(
            f"DECK_STATE_TOPIC {topic!r} isn't a topic like deck-ha-bridge/+/state; using {DEFAULT_STATE_TOPIC}"
        )
        topic = DEFAULT_STATE_TOPIC
    password = e.get("MQTT_PASSWORD") or None
    if password:
        if len(password) >= MIN_SECRET_LEN:
            register_custom_pattern(re.escape(password), "***")
        else:
            problems.append("MQTT_PASSWORD is too short to keep out of logs reliably; use a longer one")
    for p in problems:
        log.warning("%s", p)
    return MqttSettings(host, port, e.get("MQTT_USER") or None, password, tls, topic), problems


@dataclass(frozen=True)
class Settings:
    url: str = DEFAULT_URL
    dry_run: bool = False
    write_rate: int = DEFAULT_WRITE_RATE
    connect_timeout_s: float = CONNECT_TIMEOUT_S
    read_timeout_s: float = READ_TIMEOUT_S
    extra: Mapping[str, str] = field(default_factory=dict)
    mqtt: MqttSettings | None = None  # None: now playing is off
    mqtt_problems: tuple[str, ...] = ()


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
    mqtt, problems = load_mqtt(e)
    return Settings(
        url=normalize_url(e.get("BACKLOG_URL") or DEFAULT_URL),
        dry_run=flag(e.get("BACKLOG_DRY_RUN", "")),
        write_rate=rate,
        extra=extra,
        mqtt=mqtt,
        mqtt_problems=tuple(problems),
    )
