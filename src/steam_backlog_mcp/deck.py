"""Now playing on the Steam Deck: a read-only MQTT subscription to the Deck bridge's retained state.

steam-deck-ha-bridge publishes, retained, per Deck (`<node>` is its node_id):

  deck-ha-bridge/<node>/state          one JSON object (its docs/state-schema.md is the contract)
  deck-ha-bridge/<node>/availability   "online", or "offline" (its last will, or a clean stop)

DeckWatcher holds one connection, subscribes to both topics with a + for the node, and keeps the
latest message per Deck in memory. Retained messages arrive right after SUBSCRIBE, so a watcher
that has just connected knows every Deck's last state without waiting for the Deck to say anything.

Why it's shaped like this:
- **Never publishes.** No PUBLISH, and no last will either (a will is a publish the broker makes for
  us). The broker login only needs `topic read deck-ha-bridge/#`.
- **Keeps only what the tool uses** (DeckState: mode, playing, appid, game, started), not the battery
  and the rest; and at most MAX_DECKS devices, each payload at most MAX_PAYLOAD bytes, so a noisy or
  hostile publisher on the topic can't grow memory.
- **Payloads are data.** The game name is publisher-controlled text from Steam; it's stored as a
  string and only ever returned in a structured field.
- **Stale is explicit.** Each Deck remembers which connection its last message came in on (`epoch`):
  `fresh` means a message since the current connection. Disconnected, or a Deck that said offline,
  and the tool says the data may be old instead of presenting it as live.
- **Reconnects with capped backoff** (1 s doubling to 60 s, back to 1 s after a good connection), so a
  broker restart or a wrong password doesn't spin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import aiomqtt

from .config import MqttSettings
from .logsafe import mask_host_port

log = logging.getLogger(__name__)

MAX_DECKS = 8
MAX_PAYLOAD = 8192  # the bridge's state is about 400 bytes
SETTLE_S = 0.5  # after SUBACK, how long retained messages get to arrive before the watcher counts as ready
NON_STEAM_MIN = 2**31  # the bridge's schema: non-Steam shortcuts have app ids of 2^31 or more
REFUSED_LOGIN = {4, 5, 134, 135}  # CONNACK: bad user name or password, not authorized (3.1.1 and 5)

Status = Literal["starting", "connected", "login_refused", "subscribe_refused", "unreachable"]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class DeckState:
    """The fields of the bridge's state object that now playing uses, validated."""

    mode: str
    playing: bool
    appid: int | None
    game: str
    started: str | None


@dataclass
class _Device:
    state: DeckState | None = None
    received_at: datetime | None = None
    online: bool | None = None
    problem: str | None = None
    epoch: int = -1


@dataclass(frozen=True)
class DeckView:
    device: str
    state: DeckState | None
    received_at: datetime | None  # when this server got the last good state
    online: bool | None  # from the availability topic; None = no (usable) message
    fresh: bool  # a message from this Deck since the current connection
    problem: str | None  # why the last state message was ignored


def parse_state(payload: bytes) -> DeckState | str:
    """A DeckState, or the reason the payload isn't one."""
    if len(payload) > MAX_PAYLOAD:
        return f"the state message was {len(payload)} bytes, over the {MAX_PAYLOAD}-byte limit"
    try:
        data: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "the state message wasn't valid JSON"
    if not isinstance(data, dict):
        return "the state message wasn't a JSON object"
    playing, appid, game, started, mode = (data.get(k) for k in ("playing", "appid", "game", "game_started", "mode"))
    if playing not in ("ON", "OFF"):
        return "the state message's playing field isn't ON or OFF"
    if appid is not None and (isinstance(appid, bool) or not isinstance(appid, int) or appid < 0):
        return "the state message's appid isn't a whole number"
    if not isinstance(game, str):
        return "the state message has no game name"
    if started is not None and not isinstance(started, str):
        return "the state message's game_started isn't text"
    if not isinstance(mode, str):
        return "the state message has no mode"
    return DeckState(mode=mode[:40], playing=playing == "ON", appid=appid, game=game[:300], started=started)


class SubscribeRefused(Exception):
    """The broker answered SUBACK with a failure code: the login may not read the topic."""


class DeckWatcher:
    def __init__(
        self,
        settings: MqttSettings,
        *,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.backoff_initial, self.backoff_max = backoff_initial, backoff_max
        self._sleep = sleep
        self._devices: dict[str, _Device] = {}
        self._epoch = 0
        self._dropped_logged = False
        self.connected = False
        self.status: Status = "starting"
        self.ready = asyncio.Event()  # set once the first connection settled or failed
        self.endpoint = mask_host_port(f"{settings.host}:{settings.port}")
        prefix = settings.state_topic.split("/")
        self._state_pattern = prefix
        self._avail_pattern = settings.availability_topic.split("/")

    # ------------------------------------------------------------------ the cache
    def _match(self, topic: str) -> tuple[str, str] | None:
        """(device key, "state" | "availability") for a topic of ours, else None."""
        levels = topic.split("/")
        for kind, pattern in (("state", self._state_pattern), ("availability", self._avail_pattern)):
            if len(levels) == len(pattern) and all(p in ("+", t) for p, t in zip(pattern, levels, strict=True)):
                return "/".join(levels[:-1]), kind
        return None

    def handle(self, topic: str, payload: bytes) -> None:
        m = self._match(topic)
        if m is None:
            return
        key, kind = m
        dev = self._devices.get(key)
        if not payload:  # an empty retained message deletes the retained one: forget that half
            if dev is not None:
                if kind == "state":
                    dev.state, dev.received_at, dev.problem = None, None, None
                else:
                    dev.online = None
                if dev.state is None and dev.online is None and dev.problem is None:
                    del self._devices[key]
            return
        if dev is None:
            if len(self._devices) >= MAX_DECKS:
                if not self._dropped_logged:
                    log.warning("More than %d Decks publish on %s; ignoring the rest", MAX_DECKS, topic)
                    self._dropped_logged = True
                return
            dev = self._devices[key] = _Device()
        dev.epoch = self._epoch
        if kind == "availability":
            text = payload.decode("utf-8", "replace").strip()
            dev.online = True if text == "online" else False if text == "offline" else None
            return
        parsed = parse_state(payload)
        if isinstance(parsed, str):
            dev.problem = parsed
            log.warning("Deck %s: %s; keeping the last good state", key.rsplit("/", 1)[-1], parsed)
            return
        dev.state, dev.received_at, dev.problem = parsed, datetime.now(UTC), None

    def snapshot(self) -> list[DeckView]:
        return [
            DeckView(
                device=key.rsplit("/", 1)[-1],
                state=d.state,
                received_at=d.received_at,
                online=d.online,
                fresh=self.connected and d.epoch == self._epoch,
                problem=d.problem,
            )
            for key, d in sorted(self._devices.items())
        ]

    def connected_now(self) -> None:
        self._epoch += 1
        self.connected = True
        self.status = "connected"

    def disconnected(self) -> None:
        self.connected = False

    # ------------------------------------------------------------------ the connection
    def _client(self) -> aiomqtt.Client:
        s = self.settings
        return aiomqtt.Client(
            s.host,
            s.port,
            username=s.user,
            password=s.password,
            identifier=f"mcp-steam-backlog-{secrets.token_hex(4)}",  # unique: doctor and `call` connect too
            tls_context=ssl.create_default_context() if s.tls else None,
            keepalive=60,
            timeout=10,
        )

    async def _settle(self) -> None:
        await asyncio.sleep(SETTLE_S)
        self.ready.set()

    async def run(self) -> None:
        delay = self.backoff_initial
        while True:
            settle: asyncio.Task[None] | None = None
            entered = False
            try:
                async with self._client() as client:
                    entered = True
                    for topic in (self.settings.state_topic, self.settings.availability_topic):
                        if refused(await client.subscribe(topic, qos=0)):
                            raise SubscribeRefused(f"the broker refused the subscription to {topic}")
                    self.connected_now()
                    delay = self.backoff_initial
                    log.info("Watching the Steam Deck bridge on %s", self.endpoint)
                    settle = asyncio.create_task(self._settle())
                    async for message in client.messages:
                        payload = message.payload
                        if isinstance(payload, str):
                            payload = payload.encode()
                        if isinstance(payload, bytes | bytearray):
                            self.handle(message.topic.value, bytes(payload))
                    raise aiomqtt.MqttError("the connection closed")
            except SubscribeRefused as exc:
                self.status = "subscribe_refused"
                log.warning("MQTT broker %s: %s; retrying in %.0fs", self.endpoint, exc, delay)
            except aiomqtt.MqttError as exc:
                rc = getattr(exc, "rc", None)
                code = getattr(rc, "value", rc)
                # A CONNACK refusal (aiomqtt's MqttConnectError, a MqttCodeError) comes before the
                # connection is entered; a code from later on is something else.
                login = not entered and isinstance(exc, aiomqtt.MqttCodeError) and code in REFUSED_LOGIN
                self.status = "login_refused" if login else "unreachable"
                log.warning("MQTT broker %s: %s; retrying in %.0fs", self.endpoint, exc, delay)
            finally:
                if settle is not None:
                    settle.cancel()
                self.disconnected()
            self.ready.set()
            await self._sleep(delay)
            delay = min(delay * 2, self.backoff_max)

    async def wait_ready(self, timeout: float) -> bool:
        try:
            async with asyncio.timeout(timeout):
                await self.ready.wait()
        except TimeoutError:
            return False
        return True


def refused(codes: Any) -> bool:
    """SUBACK return codes: ints (0-2 granted, 0x80 failure) or paho ReasonCodes."""
    for c in codes:
        failure = getattr(c, "is_failure", None)
        if failure if failure is not None else int(c) >= 0x80:
            return True
    return False
