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
    rgb: Optional[tuple] = None
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
        rgb: Optional[tuple] = None,
        is_on: bool = True,
    ) -> None:
        with self._lock:
            self.light = LightSnapshot(
                brightness=brightness,
                color_temp=color_temp,
                rgb=rgb,
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
                    "rgb": list(self.light.rgb) if self.light.rgb else None,
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


def create_app(state: DashboardState, neuro=None) -> Flask:
    """Flask app factory.

    Parameters
    ----------
    state : DashboardState
        Shared brain/light state between main loop and web threads.
    neuro : NeuroLux or None
        Reference to main app instance for pause/resume control.
        If None, /api/pause returns 503 (pause not available).
    """
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
        rgb = data.get("rgb")
        # Clear color_temp if RGB is specified (mutually exclusive on WiZ)
        if rgb is not None:
            color_temp = None
        if brightness is not None or color_temp is not None or rgb is not None:
            # Signal the main loop via a module-level flag
            _pending_override.update({
                "brightness": brightness,
                "color_temp": color_temp,
                "rgb": rgb,
                "pending": True,
            })
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "missing params"}), 400

    # ── presets ────────────────────────────────────────────────────

    @app.route("/api/presets")
    def api_presets():
        return jsonify(LIGHT_PRESETS)

    @app.route("/api/presets/apply", methods=["POST"])
    def api_presets_apply():
        """Apply a named light preset."""
        data = request.get_json(force=True) if request.data else {}
        key = data.get("preset", "")
        preset = LIGHT_PRESETS.get(key)
        if preset is None:
            return jsonify({"ok": False, "error": f"unknown preset: {key}"}), 400
        _pending_override.update({
            "brightness": preset["brightness"],
            "color_temp": preset["color_temp"],
            "pending": True,
        })
        return jsonify({"ok": True, "preset": preset})

    # ── RGB colors ─────────────────────────────────────────────────

    @app.route("/api/rgb-colors")
    def api_rgb_colors():
        return jsonify(RGB_COLORS)

    @app.route("/api/rgb/apply", methods=["POST"])
    def api_rgb_apply():
        """Apply an RGB color by name or raw [r,g,b] array."""
        data = request.get_json(force=True) if request.data else {}
        key = data.get("color", "")
        rgb = data.get("rgb")
        brightness = data.get("brightness")

        if key and key in RGB_COLORS:
            rgb = RGB_COLORS[key]["rgb"]

        if rgb is None or len(rgb) != 3:
            return jsonify({"ok": False, "error": "need color name or rgb [r,g,b]"}), 400

        _pending_override.update({
            "brightness": brightness or 80,
            "color_temp": None,
            "rgb": tuple(int(c) for c in rgb),
            "pending": True,
        })
        return jsonify({"ok": True, "rgb": list(rgb)})

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

    # ── state colors config ──────────────────────────────────────────

    @app.route("/api/states-config")
    def api_states_config():
        """Return the current brain-state → light mapping."""
        return jsonify(STATE_COLORS)

    @app.route("/api/states-config/set", methods=["POST"])
    def api_states_config_set():
        """Update one or more state color entries.

        Body: {"open_focused": {"brightness": 90, "rgb": [255,50,0]}, ...}
        Saves to config.yaml + updates the live mapper immediately.
        """
        data = request.get_json(force=True) if request.data else {}
        updated = []
        for key, val in data.items():
            if key not in STATE_COLORS:
                continue
            if not isinstance(val, dict):
                continue
            STATE_COLORS[key].update(val)
            # Validate rgb
            if "rgb" in val:
                STATE_COLORS[key]["rgb"] = [
                    max(0, min(255, int(c))) for c in val["rgb"]
                ]
            updated.append(key)

        if not updated:
            return jsonify({"ok": False, "error": "no valid keys"}), 400

        # Update the live mapper if available
        if _live_mapper is not None:
            _live_mapper.reload_table()

        # Persist to disk
        _save_state_colors_to_yaml()

        return jsonify({"ok": True, "updated": updated})

    # ── pause / resume ─────────────────────────────────────────────

    @app.route("/api/pause", methods=["GET", "POST"])
    def api_pause():
        """GET: return current pause state.  POST: set or toggle pause."""
        if neuro is None:
            return jsonify({"paused": False, "error": "pause not available"}), 503

        if request.method == "POST":
            data = request.get_json(force=True) if request.data else {}
            if "paused" in data:
                neuro._paused = bool(data["paused"])
            else:
                neuro._paused = not neuro._paused  # toggle with no body

        return jsonify({"paused": neuro._paused})

    @app.route("/app")
    def app_page():
        return render_template("app.html")

    return app


# Module-level override flags (read by main loop)
_pending_override: dict = {"pending": False, "brightness": None, "color_temp": None, "rgb": None}

# ── light presets ─────────────────────────────────────────────────────

LIGHT_PRESETS = {
    "focus": {
        "name": "专注", "icon": "🎯",
        "brightness": 100, "color_temp": 5500,
        "desc": "高亮冷白光，提升专注力",
    },
    "relax": {
        "name": "放松", "icon": "🌿",
        "brightness": 60, "color_temp": 3200,
        "desc": "柔和暖光，放松身心",
    },
    "reading": {
        "name": "阅读", "icon": "📖",
        "brightness": 80, "color_temp": 4200,
        "desc": "中性自然光，舒适阅读",
    },
    "sleep": {
        "name": "睡眠", "icon": "🌙",
        "brightness": 10, "color_temp": 2700,
        "desc": "极暗暖光，助眠模式",
    },
    "night": {
        "name": "夜灯", "icon": "💤",
        "brightness": 5, "color_temp": 2200,
        "desc": "微光不刺眼，夜间照明",
    },
}

# ── RGB color presets ──────────────────────────────────────────────────

RGB_COLORS = {
    "red":    {"name": "红", "rgb": [255, 0, 0]},
    "orange": {"name": "橙", "rgb": [255, 100, 0]},
    "yellow": {"name": "黄", "rgb": [255, 200, 0]},
    "green":  {"name": "绿", "rgb": [0, 255, 0]},
    "cyan":   {"name": "青", "rgb": [0, 255, 200]},
    "blue":   {"name": "蓝", "rgb": [0, 50, 255]},
    "purple": {"name": "紫", "rgb": [128, 0, 255]},
    "pink":   {"name": "粉", "rgb": [255, 50, 150]},
    "warm":   {"name": "暖白", "rgb": [255, 180, 100]},
    "cool":   {"name": "冷白", "rgb": [200, 210, 255]},
}

# ── brain-state → light mapping (configurable, persisted to config.yaml) ─

STATE_COLORS: dict[str, dict] = {
    "open_focused": {
        "label": "睁眼·专注", "icon": "👁",
        "brightness": 85, "rgb": [255, 0, 0], "transition_s": 0.8,
    },
    "open_relaxed": {
        "label": "睁眼·普通", "icon": "👀",
        "brightness": 65, "rgb": [255, 30, 0], "transition_s": 1.0,
    },
    "transition": {
        "label": "过渡状态", "icon": "🌓",
        "brightness": 40, "rgb": [0, 255, 0], "transition_s": 1.5,
    },
    "closed": {
        "label": "闭眼·放松", "icon": "😌",
        "brightness": 20, "rgb": [0, 0, 255], "transition_s": 2.0,
    },
    "long_closed": {
        "label": "长闭·入睡", "icon": "😴",
        "brightness": 0, "rgb": [0, 0, 30], "transition_s": 5.0,
    },
}

# Keep a reference to the mapper for live updates
_live_mapper: object = None


def _load_state_colors_from_yaml() -> None:
    """Merge any saved state colors from config.yaml."""
    import sys
    from pathlib import Path
    project_root = Path(__file__).resolve().parent.parent
    config_path = project_root / "config.yaml"
    if not config_path.exists():
        return
    try:
        import yaml
    except ImportError:
        return
    try:
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh) or {}
        saved = cfg.get("state_colors")
        if isinstance(saved, dict):
            for key, val in saved.items():
                if key in STATE_COLORS and isinstance(val, dict):
                    STATE_COLORS[key].update(val)
            logger.info("Loaded state colors from config.yaml")
    except Exception as exc:
        logger.warning("Failed to load state colors: %s", exc)


def _save_state_colors_to_yaml() -> None:
    """Persist current state colors to config.yaml."""
    import sys
    from pathlib import Path
    project_root = Path(__file__).resolve().parent.parent
    config_path = project_root / "config.yaml"
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed — state colors not persisted")
        return
    try:
        cfg: dict = {}
        if config_path.exists():
            with open(config_path) as fh:
                cfg = yaml.safe_load(fh) or {}
        cfg["state_colors"] = STATE_COLORS
        with open(config_path, "w") as fh:
            yaml.dump(cfg, fh, default_flow_style=False, allow_unicode=True)
        logger.info("State colors saved to config.yaml")
    except Exception as exc:
        logger.warning("Failed to save state colors: %s", exc)


# Load saved config on import
_load_state_colors_from_yaml()
