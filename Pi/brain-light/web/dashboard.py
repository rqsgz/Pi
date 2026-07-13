#!/usr/bin/env python3
"""Flask web dashboard + Server-Sent Events (SSE) real-time brain-wave feed.

Provides:
    GET  /              →  dashboard HTML page
    GET  /api/state     →  latest brain state as JSON
    GET  /api/history   →  last N brain frames (query: ?n=200)
    GET  /api/light     →  current light state
    POST /api/light/set →  manually override light
    GET  /api/stream    →  SSE event stream (text/event-stream, 500 ms)

Architecture
------------
A shared ``DashboardState`` singleton holds the latest frame + light
status.  The main application loop pushes updates into it; the Flask
SSE generator polls it at 2 Hz.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Optional

from flask import Flask, Response, jsonify, render_template, request

logger = logging.getLogger(__name__)

# ── shared state (thread-safe) ─────────────────────────────────────────


@dataclass
class LightSnapshot:
    brightness: int = 65
    color_temp: int = 4200
    is_on: bool = True
    state_label: str = "—"


class DashboardState:
    """Singleton shared between main loop and Flask SSE threads."""

    def __init__(self, history_len: int = 300) -> None:
        self._lock = Lock()

        # Latest brain frame
        self.attention: float = 0.0
        self.meditation: float = 0.0
        self.alpha_power: float = 0.0
        self.poor_signal: int = 0
        self.eyes_state: str = "open"
        self.blink_count: int = 0
        self.eeg_bands: dict[str, float] = {}
        self.last_update: float = 0.0

        # Light
        self.light = LightSnapshot()

        # Rolling history (ring buffer)
        self._history: deque[dict] = deque(maxlen=history_len)

    # ── write ──────────────────────────────────────────────────────

    def update_brain(
        self,
        attention: float,
        meditation: float,
        alpha_power: float,
        poor_signal: int,
        eyes_state: str,
        blink_count: int = 0,
        eeg_bands: Optional[dict] = None,
    ) -> None:
        now = time.time()

        # ── data validation: reject obviously bad values ────────────
        # TGAM eSense is 0-100; anything outside is a parse error
        if not (0 <= attention <= 100):
            attention = 0.0
        if not (0 <= meditation <= 100):
            meditation = 0.0
        # alpha_power above 50k is almost certainly garbage
        if alpha_power > 50000 or alpha_power < 0:
            alpha_power = 0.0

        with self._lock:
            self.attention = attention
            self.meditation = meditation
            self.alpha_power = alpha_power
            self.poor_signal = poor_signal
            self.eyes_state = eyes_state
            self.blink_count = blink_count
            self.eeg_bands = eeg_bands or {}
            self.last_update = now

            self._history.append({
                "ts": now,
                "attention": attention,
                "meditation": meditation,
                "alpha_power": alpha_power,
                "poor_signal": poor_signal,
                "eyes_state": eyes_state,
            })

    def update_light(
        self,
        brightness: int,
        color_temp: int,
        state_label: str,
        is_on: bool = True,
    ) -> None:
        with self._lock:
            self.light = LightSnapshot(
                brightness=brightness,
                color_temp=color_temp,
                is_on=is_on,
                state_label=state_label,
            )

    # ── read ───────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "attention": self.attention,
                "meditation": self.meditation,
                "alpha_power": self.alpha_power,
                "poor_signal": self.poor_signal,
                "eyes_state": self.eyes_state,
                "blink_count": self.blink_count,
                "eeg_bands": dict(self.eeg_bands),
                "light": {
                    "brightness": self.light.brightness,
                    "color_temp": self.light.color_temp,
                    "is_on": self.light.is_on,
                    "state_label": self.light.state_label,
                },
            }

    def history(self, n: int = 200) -> list[dict]:
        with self._lock:
            items = list(self._history)[-n:]
        # Make JSON-serialisable
        return [
            {
                "ts": f"{h['ts']:.3f}",
                "attention": round(h["attention"], 1),
                "meditation": round(h["meditation"], 1),
                "alpha_power": round(h["alpha_power"], 1),
                "poor_signal": h["poor_signal"],
                "eyes_state": h["eyes_state"],
            }
            for h in items
        ]


# ── Flask app factory ──────────────────────────────────────────────────


def create_app(state: DashboardState) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # disable static cache

    # ── REST API ─────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/state")
    def api_state():
        return jsonify(state.snapshot())

    @app.route("/api/history")
    def api_history():
        n = request.args.get("n", 200, type=int)
        return jsonify(state.history(n=min(n, 500)))

    @app.route("/api/light")
    def api_light():
        snap = state.snapshot()["light"]
        return jsonify(snap)

    @app.route("/api/light/set", methods=["POST"])
    def api_light_set():
        """Manual override endpoint — queues a one-shot light command.

        The main loop polls ``_pending_override``; if set, it applies
        the command and clears it.
        """
        data = request.get_json(force=True) if request.data else {}
        brightness = data.get("brightness")
        color_temp = data.get("color_temp")
        if brightness is not None or color_temp is not None:
            # Signal the main loop via a module-level flag
            _pending_override.update({
                "brightness": brightness,
                "color_temp": color_temp,
                "pending": True,
            })
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "missing params"}), 400

    # ── SSE stream ───────────────────────────────────────────────────

    @app.route("/api/stream")
    def api_stream():
        def event_stream():
            while True:
                snap = state.snapshot()
                payload = json.dumps(snap, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                time.sleep(0.15)

        return Response(
            event_stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Access-Control-Allow-Origin": "*",
            },
        )

    return app


# Module-level override flags (read by main loop)
_pending_override: dict = {"pending": False, "brightness": None, "color_temp": None}
