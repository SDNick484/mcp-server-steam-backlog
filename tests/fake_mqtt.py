"""A small MQTT 3.1.1 broker for tests, on a loopback port in its own thread.

Enough of the protocol for aiomqtt (paho) as a subscriber: CONNECT with a username and password
(CONNACK 5 when they don't match), SUBSCRIBE with per-filter refusals (SUBACK 0x80), retained
messages delivered on subscribe, wildcard matching (+ and #), PUBLISH in both directions, PINGREQ,
DISCONNECT. A read-only ACL is imitated the way Mosquitto enforces one: a refused PUBLISH is
acknowledged and silently dropped.

It runs in a thread with its own event loop, so both async tests and the command line (which calls
asyncio.run itself) can talk to it. Interact through the thread-safe methods: publish(), drop().
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import threading
from dataclasses import dataclass, field
from typing import Any


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, level in enumerate(p):
        if level == "#":
            return True
        if i >= len(t) or (level != "+" and level != t[i]):
            return False
    return len(p) == len(t)


def _remaining(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _string(s: str) -> bytes:
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def publish_packet(topic: str, payload: bytes, retain: bool) -> bytes:
    body = _string(topic) + payload
    return bytes([0x30 | (1 if retain else 0)]) + _remaining(len(body)) + body


@dataclass
class Conn:
    writer: asyncio.StreamWriter
    client_id: str = ""
    user: str | None = None
    subs: list[str] = field(default_factory=list)


class FakeBroker:
    def __init__(self, *, user: str | None = None, password: str | None = None) -> None:
        self.user, self.password = user, password
        self.retained: dict[str, bytes] = {}
        self.deny_subscribe: set[str] = set()  # topic filters answered with 0x80
        self.read_only = True  # client PUBLISHes are acknowledged and dropped (a read-only ACL)
        self.connects: list[dict[str, Any]] = []
        self.client_publishes: list[tuple[str, bytes, bool]] = []  # every PUBLISH a client sent
        self.port = 0
        self._conns: list[Conn] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle (call from tests)
    def start(self) -> FakeBroker:
        ready = threading.Event()

        def run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop

            async def boot() -> None:
                self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
                self.port = self._server.sockets[0].getsockname()[1]
                ready.set()

            loop.run_until_complete(boot())
            loop.run_forever()
            loop.close()

        self._thread = threading.Thread(target=run, name="fake-mqtt", daemon=True)
        self._thread.start()
        ready.wait(5)
        return self

    def stop(self) -> None:
        if self._loop is None:
            return

        async def shutdown() -> None:
            await self._drop()
            if self._server:
                self._server.close()

        asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(5)
        self._loop = None

    def __enter__(self) -> FakeBroker:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _run(self, coro: Any) -> Any:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(5)

    # ------------------------------------------------------------------ what a test does to it
    def publish(self, topic: str, payload: str | bytes, retain: bool = True) -> None:
        """A message from another client (the Deck bridge): stored if retained, sent to subscribers."""
        data = payload.encode() if isinstance(payload, str) else payload
        self._run(self._route(topic, data, retain))

    def drop(self) -> None:
        """Close every client connection, as a broker restart would."""
        self._run(self._drop())

    def subscriptions(self) -> list[str]:
        return [s for c in self._conns for s in c.subs]

    # ------------------------------------------------------------------ the broker
    async def _drop(self) -> None:
        for c in self._conns:
            c.writer.close()
        self._conns.clear()

    async def _route(self, topic: str, payload: bytes, retain: bool) -> None:
        if retain:
            if payload:
                self.retained[topic] = payload
            else:
                self.retained.pop(topic, None)
        for c in list(self._conns):
            if any(topic_matches(s, topic) for s in c.subs):
                c.writer.write(publish_packet(topic, payload, False))
                with contextlib.suppress(ConnectionError):
                    await c.writer.drain()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = Conn(writer)
        try:
            while True:
                header = (await reader.readexactly(1))[0]
                length, mult = 0, 1
                while True:
                    b = (await reader.readexactly(1))[0]
                    length += (b & 0x7F) * mult
                    mult *= 128
                    if not b & 0x80:
                        break
                body = await reader.readexactly(length)
                kind = header >> 4
                if kind == 1:  # CONNECT
                    info = self._parse_connect(body)
                    self.connects.append(info)
                    if self.user is not None and (info.get("user"), info.get("password")) != (self.user, self.password):
                        writer.write(b"\x20\x02\x00\x05")  # not authorized
                        await writer.drain()
                        return
                    conn.client_id, conn.user = info["client_id"], info.get("user")
                    self._conns.append(conn)
                    writer.write(b"\x20\x02\x00\x00")
                elif kind == 8:  # SUBSCRIBE
                    pid = body[:2]
                    rest, codes, granted = body[2:], [], []
                    while rest:
                        n = struct.unpack("!H", rest[:2])[0]
                        flt, qos = rest[2 : 2 + n].decode(), rest[2 + n]
                        rest = rest[3 + n :]
                        if flt in self.deny_subscribe:
                            codes.append(0x80)
                        else:
                            codes.append(min(qos, 1))
                            granted.append(flt)
                    conn.subs.extend(granted)
                    writer.write(bytes([0x90]) + _remaining(2 + len(codes)) + pid + bytes(codes))
                    for flt in granted:
                        for t, p in sorted(self.retained.items()):
                            if topic_matches(flt, t):
                                writer.write(publish_packet(t, p, True))
                elif kind == 3:  # PUBLISH from a client
                    n = struct.unpack("!H", body[:2])[0]
                    topic = body[2 : 2 + n].decode()
                    qos = (header >> 1) & 3
                    rest = body[2 + n :]
                    if qos:
                        writer.write(b"\x40\x02" + rest[:2])
                        rest = rest[2:]
                    retain = bool(header & 1)
                    self.client_publishes.append((topic, rest, retain))
                    if not self.read_only:
                        await self._route(topic, rest, retain)
                elif kind == 12:  # PINGREQ
                    writer.write(b"\xd0\x00")
                elif kind == 10:  # UNSUBSCRIBE
                    writer.write(b"\xb0\x02" + body[:2])
                elif kind == 14:  # DISCONNECT
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            if conn in self._conns:
                self._conns.remove(conn)
            writer.close()

    @staticmethod
    def _parse_connect(body: bytes) -> dict[str, Any]:
        n = struct.unpack("!H", body[:2])[0]
        flags = body[2 + n + 1]
        rest = body[2 + n + 4 :]
        fields: list[bytes] = []
        while rest:
            ln = struct.unpack("!H", rest[:2])[0]
            fields.append(rest[2 : 2 + ln])
            rest = rest[2 + ln :]
        out: dict[str, Any] = {"client_id": fields.pop(0).decode(), "will": None}
        if flags & 0x04:
            out["will"] = (fields.pop(0).decode(), fields.pop(0).decode())
        if flags & 0x80:
            out["user"] = fields.pop(0).decode()
        if flags & 0x40:
            out["password"] = fields.pop(0).decode()
        return out
