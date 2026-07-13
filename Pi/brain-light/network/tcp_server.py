#!/usr/bin/env python3
"""Async TCP server — broadcasts brain-wave state to LAN clients.

Listens on 0.0.0.0:9527.  Each client receives JSON-Line messages
delimited by ``\\n`` as soon as new brain/light state is available.

Message types
-------------
    brain_state   — every ~0.5 s  (attention, meditation, alpha, eyes, …)
    light_status  — on every light change
    ping / pong   — keep-alive (5 s / 15 s timeout)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9527
PING_INTERVAL = 5.0       # seconds between pings
CLIENT_TIMEOUT = 15.0      # seconds without pong → disconnect

# ── message builders ────────────────────────────────────────────────────


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def build_brain_state(
    attention: float,
    meditation: float,
    alpha_power: float,
    poor_signal: int,
    eyes_state: str,
    blink_count: int = 0,
    eeg_bands: Optional[dict] = None,
) -> bytes:
    msg = {
        "type": "brain_state",
        "ts": _now_iso(),
        "attention": round(attention, 1),
        "meditation": round(meditation, 1),
        "alpha_power": round(alpha_power, 1),
        "poor_signal": poor_signal,
        "eyes_state": eyes_state,
        "blink_count": blink_count,
    }
    if eeg_bands:
        msg["eeg_bands"] = {k: round(v, 1) for k, v in eeg_bands.items()}
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def build_light_status(
    brightness: int,
    color_temp: int,
    state_label: str,
    is_on: bool = True,
) -> bytes:
    msg = {
        "type": "light_status",
        "ts": _now_iso(),
        "brightness": brightness,
        "color_temp": color_temp,
        "state_label": state_label,
        "is_on": is_on,
    }
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")


def build_ping() -> bytes:
    return b'{"type":"ping","ts":"' + _now_iso().encode() + b'"}\n'


def build_pong() -> bytes:
    return b'{"type":"pong","ts":"' + _now_iso().encode() + b'"}\n'


# ── server ──────────────────────────────────────────────────────────────


class BrainServer:
    """Async TCP brain-wave broadcast server.

    Parameters
    ----------
    host : str
        Bind address (default "0.0.0.0").
    port : int
        Bind port (default 9527).
    ping_interval : float
        Seconds between keep-alive pings.
    client_timeout : float
        Seconds of silence before client is dropped.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        ping_interval: float = PING_INTERVAL,
        client_timeout: float = CLIENT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.ping_interval = ping_interval
        self.client_timeout = client_timeout

        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: dict[asyncio.StreamWriter, float] = {}  # writer → last_pong
        self._broadcast_queue: Optional[asyncio.Queue[bytes]] = None
        self._running = False

    # ── lifecycle ───────────────────────────────────────────────────

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start listening and background tasks."""
        if self._broadcast_queue is None:
            self._broadcast_queue = asyncio.Queue(maxsize=256)
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        self._running = True
        logger.info("TCP brain server listening on %s:%d", self.host, self.port)

        # Background: broadcast worker + heartbeat
        asyncio.create_task(self._broadcast_worker())
        asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False

        # Disconnect all clients
        for writer in list(self._clients):
            try:
                writer.close()
            except Exception:
                pass
        self._clients.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        logger.info("TCP brain server stopped")

    # ── broadcast API ────────────────────────────────────────────────

    def broadcast(self, data: bytes) -> None:
        """Enqueue raw bytes for broadcast to all connected clients."""
        if self._broadcast_queue is None:
            return
        try:
            self._broadcast_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("Broadcast queue full — dropping frame")

    def broadcast_brain_state(
        self,
        attention: float,
        meditation: float,
        alpha_power: float,
        poor_signal: int,
        eyes_state: str,
        blink_count: int = 0,
        eeg_bands: Optional[dict] = None,
    ) -> None:
        self.broadcast(
            build_brain_state(
                attention, meditation, alpha_power,
                poor_signal, eyes_state, blink_count, eeg_bands,
            )
        )

    def broadcast_light_status(
        self,
        brightness: int,
        color_temp: int,
        state_label: str,
        is_on: bool = True,
    ) -> None:
        self.broadcast(build_light_status(brightness, color_temp, state_label, is_on))

    # ── internals ────────────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        addr = writer.get_extra_info("peername", "?")
        logger.info("TCP client connected: %s", addr)
        self._clients[writer] = time.time()

        try:
            while self._running:
                try:
                    line = await asyncio.wait_for(
                        reader.readline(), timeout=self.client_timeout
                    )
                except asyncio.TimeoutError:
                    logger.info("TCP client timeout: %s", addr)
                    break

                if not line:  # EOF
                    break

                # Handle incoming messages (pong, etc.)
                try:
                    msg = json.loads(line.decode("utf-8").strip())
                    if msg.get("type") == "pong":
                        self._clients[writer] = time.time()
                except json.JSONDecodeError:
                    pass
        except Exception as exc:
            logger.debug("TCP client error %s: %s", addr, exc)
        finally:
            self._clients.pop(writer, None)
            try:
                writer.close()
            except Exception:
                pass
            logger.info("TCP client disconnected: %s", addr)

    async def _broadcast_worker(self) -> None:
        """Consume the broadcast queue and send to all clients."""
        while self._running:
            try:
                data = await asyncio.wait_for(
                    self._broadcast_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            dead: list[asyncio.StreamWriter] = []
            for writer in list(self._clients):
                try:
                    writer.write(data)
                    await writer.drain()
                except Exception:
                    dead.append(writer)

            for w in dead:
                self._clients.pop(w, None)
                try:
                    w.close()
                except Exception:
                    pass

    async def _heartbeat_loop(self) -> None:
        """Ping clients periodically; drop those that don't pong."""
        while self._running:
            await asyncio.sleep(self.ping_interval)

            now = time.time()
            dead: list[asyncio.StreamWriter] = []

            for writer, last_seen in list(self._clients.items()):
                if now - last_seen > self.client_timeout:
                    dead.append(writer)
                else:
                    try:
                        writer.write(build_ping())
                        await writer.drain()
                    except Exception:
                        dead.append(writer)

            for w in dead:
                self._clients.pop(w, None)
                try:
                    w.close()
                except Exception:
                    pass

            if dead:
                logger.debug("Cleaned up %d stale client(s)", len(dead))
