#!/usr/bin/env python3
"""NeuroLux — 驭光 · 脑波驱灯 · 意念驭光

Unified entry point that wires together:
    TGAM Bluetooth → parser → signal pipeline → classifier → mapper
                                                   ↓
                                  ┌────────────────┼────────────────┐
                                  ↓                ↓                ↓
                             WiZ UDP bulb    GPIO LED PWM    TCP :9527 broadcast
                                                                     ↓
                                                            Flask :5000 dashboard

Usage
-----
    python3 main.py                          # live TGAM → WiZ + TCP + Web
    python3 main.py --replay data/session.csv  # offline CSV replay
    python3 main.py --no-light --no-tcp        # brain-only debugging
    python3 main.py --calibrate                # personal alpha baseline
    python3 main.py --config my_config.yaml    # custom config
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# ── project-root on sys.path ──────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from tgam.parser import ThinkGearParser, EEG_BANDS
from tgam.replayer import Replayer, write_frame_row
from signal.processor import SignalProcessor
from signal.blink_detector import BlinkDetector
from signal.classifier import StateClassifier, EyesState
from light.gpio_led import GpioLed
from light.wiz_controller import WiZController
from light.mapper import LightMapper
from network.tcp_server import BrainServer
from web.dashboard import DashboardState, create_app, _pending_override
import socket as _socket

# ── logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("neuroflux")

# ── suppress noisy third-party loggers ─────────────────────────────────
for _noisy in ("werkzeug", "urllib3", "engineio", "socketio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════
#  config layer
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "serial": {"port": "/dev/rfcomm0", "baudrate": 57600, "timeout": 0.5},
    "wiz": {"ip": None, "port": 38899, "timeout": 5.0},
    "tcp": {"enabled": True, "host": "0.0.0.0", "port": 9527},
    "web": {"enabled": True, "host": "0.0.0.0", "port": 5000},
    "signal": {
        "attn_window": 10,
        "alpha_window": 20,
        "alpha_close_thresh": 150.0,
        "alpha_open_thresh": 60.0,
        "attn_close_thresh": 45.0,
        "attn_open_thresh": 55.0,
        "confirm_time": 1.0,
        "cooldown": 3.0,
        "sleep_time": 60.0,
    },
    "replay": {"speed": 1.0, "loop": False},
    "logging": {"level": "INFO"},
}


def load_config(path: Optional[str] = None) -> dict:
    """Load YAML config if available, falling back to defaults."""
    cfg = dict(DEFAULT_CONFIG)

    yaml_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    if yaml_path.exists():
        try:
            import yaml
            with open(yaml_path) as fh:
                user = yaml.safe_load(fh) or {}
            _deep_merge(cfg, user)
            logger.info("Loaded config from %s", yaml_path)
        except ImportError:
            logger.warning("PyYAML not installed — using defaults")
        except Exception as exc:
            logger.warning("Config parse error (%s) — using defaults", exc)
    return cfg


def _deep_merge(base: dict, overlay: dict) -> None:
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ═══════════════════════════════════════════════════════════════════════
#  main loop
# ═══════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════
#  Bluetooth socket stream (drop-in replacement for serial.Serial)
# ═══════════════════════════════════════════════════════════════════════

class BTSocketStream:
    """Drop-in replacement for pyserial.Serial using native AF_BLUETOOTH.

    Usage:  stream = BTSocketStream("04:22:12:02:0D:C0", channel=1)
    """

    def __init__(self, mac: str, channel: int = 1, timeout: float = 0.5):
        self._mac = mac
        self._channel = channel
        self._timeout = timeout
        self._sock: Optional[_socket.socket] = None
        self._buf = bytearray()
        self.port = f"bt:{mac}"
        self._connect()

    def _connect(self) -> None:
        self._sock = _socket.socket(
            _socket.AF_BLUETOOTH, _socket.SOCK_STREAM, _socket.BTPROTO_RFCOMM
        )
        self._sock.settimeout(self._timeout)
        self._sock.connect((self._mac, self._channel))
        self._sock.setblocking(False)

    @property
    def in_waiting(self) -> int:
        """Return buffered bytes + peek at socket."""
        try:
            chunk = self._sock.recv(4096)
            if chunk:
                self._buf.extend(chunk)
        except (BlockingIOError, _socket.timeout):
            pass
        return len(self._buf)

    def read(self, size: int = 64) -> bytes:
        """Read up to *size* bytes (emulates serial.read)."""
        # Top-up from socket
        try:
            chunk = self._sock.recv(max(size, 4096))
            if chunk:
                self._buf.extend(chunk)
        except (BlockingIOError, _socket.timeout):
            pass

        if not self._buf:
            # Block briefly for initial data
            try:
                self._sock.settimeout(self._timeout)
                chunk = self._sock.recv(size)
                self._sock.setblocking(False)
                if chunk:
                    self._buf.extend(chunk)
            except (_socket.timeout, BlockingIOError):
                pass

        take = min(size, len(self._buf))
        data = bytes(self._buf[:take])
        del self._buf[:take]
        return data

    def write(self, data: bytes) -> None:
        """Send data to the TGAM (e.g. init command)."""
        try:
            self._sock.sendall(data)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


class NeuroLux:
    """Top-level application orchestrator."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.cfg = load_config(args.config)

        # Shared state for web dashboard
        self.dash_state = DashboardState()

        # Components (initialised in setup())
        self.parser = ThinkGearParser()
        self.processor: Optional[SignalProcessor] = None
        self.blink_detector: Optional[BlinkDetector] = None
        self.classifier: Optional[StateClassifier] = None
        self.mapper: Optional[LightMapper] = None
        self.gpio_led: Optional[GpioLed] = None
        self.wiz_ctrl: Optional[WiZController] = None
        self.tcp_server: Optional[BrainServer] = None

        self._replayer: Optional[Replayer] = None
        self._csv_writer: Optional[csv.DictWriter] = None
        self._csv_fh = None
        self._running = False
        self._loop = None  # asyncio event loop reference
        self._serial = None
        self._serial_errors = 0        # consecutive read errors
        self._last_reconnect = 0.0      # throttle reconnection attempts

    # ── setup ───────────────────────────────────────────────────────

    def setup(self) -> None:
        """Instantiate all pipeline components."""
        sig = self.cfg["signal"]

        self.processor = SignalProcessor(
            attn_window=sig["attn_window"],
            alpha_window=sig["alpha_window"],
        )
        self.blink_detector = BlinkDetector()
        self.classifier = StateClassifier(
            alpha_close_thresh=sig["alpha_close_thresh"],
            alpha_open_thresh=sig["alpha_open_thresh"],
            attn_close_thresh=sig["attn_close_thresh"],
            attn_open_thresh=sig["attn_open_thresh"],
            confirm_time=sig["confirm_time"],
            cooldown=sig["cooldown"],
            sleep_time=sig["sleep_time"],
            inverted=sig.get("inverted", False),
        )
        self.mapper = LightMapper()

        # GPIO LED
        if not self.args.no_light:
            self.gpio_led = GpioLed(simulate=self.args.simulate_gpio)

        # WiZ controller
        if not self.args.no_light:
            wiz_cfg = self.cfg["wiz"]
            self.wiz_ctrl = WiZController(
                bulb_ip=wiz_cfg.get("ip"),
                port=wiz_cfg["port"],
                timeout=wiz_cfg["timeout"],
            )
            if wiz_cfg.get("ip") is None:
                logger.info("Discovering WiZ bulbs…")
                bulbs = self.wiz_ctrl.discover()
                if bulbs:
                    first_mac = next(iter(bulbs))
                    self.wiz_ctrl._ip = bulbs[first_mac].ip
                    logger.info("Using WiZ bulb at %s", self.wiz_ctrl._ip)

        # TCP server (created in setup, started in run_async)
        tcp_cfg = self.cfg["tcp"]
        if not self.args.no_tcp and tcp_cfg.get("enabled", True):
            self.tcp_server = BrainServer(
                host=tcp_cfg["host"],
                port=tcp_cfg["port"],
            )

    # ── data source ─────────────────────────────────────────────────

    def _open_source(self):
        """Open the data source: either serial port, Bluetooth socket, or CSV replayer."""
        if self.args.replay:
            self._replayer = Replayer(
                self.args.replay,
                speed=self.cfg["replay"]["speed"],
                loop=self.cfg["replay"]["loop"],
            )
            return

        ser_cfg = self.cfg["serial"]
        port = ser_cfg["port"]

        # Bluetooth direct socket (bypasses broken rfcomm)
        if port.startswith("bt:"):
            # Format: bt:<mac_with_colons>:<channel>
            # Example: bt:04:22:12:02:0D:C0:1
            raw = port[3:]  # strip "bt:" prefix
            parts = raw.split(":")
            # MAC is 6 hex octets (first 6 parts), channel is optional last
            if len(parts) >= 7 and parts[-1].isdigit():
                mac = ":".join(parts[:6])
                channel = int(parts[-1])
            else:
                mac = ":".join(parts[:6])
                channel = 1
            self._serial = BTSocketStream(mac, channel, timeout=ser_cfg["timeout"])
            import time as _time
            # TGAM init: trigger full data output (raw + eSense + EEG power)
            self._serial.write(bytes([0xAA, 0x00]))
            _time.sleep(0.1)
            logger.info("Bluetooth socket %s channel %d", mac, channel)
            return

        # Live TGAM via serial
        import serial as pyserial
        self._serial = pyserial.Serial(
            port=port,
            baudrate=ser_cfg["baudrate"],
            timeout=ser_cfg["timeout"],
        )
        # TGAM init: trigger full data output (raw + eSense + EEG power)
        import time as _time
        self._serial.write(bytes([0xAA, 0x00]))
        _time.sleep(0.1)
        logger.info("Serial port %s opened @ %d bps", port, ser_cfg["baudrate"])

    def _open_csv_writer(self) -> None:
        """Open CSV output file for session recording."""
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = PROJECT_ROOT / "data" / f"session_{ts}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._csv_fh = open(path, "w", newline="", encoding="utf-8")
        cols = ["ts", "attention", "meditation", "poor_signal", "raw_wave"] + EEG_BANDS
        self._csv_writer = csv.DictWriter(self._csv_fh, fieldnames=cols)
        self._csv_writer.writeheader()
        logger.info("Recording session → %s", path)

    # ── run ─────────────────────────────────────────────────────────

    def run(self) -> None:
        """Synchronous entry point (wraps asyncio)."""
        self.setup()
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        """Async main loop: read frames → process → control → broadcast."""
        self._running = True

        # Start TCP server
        if self.tcp_server is not None:
            await self.tcp_server.start()

        # Start Flask in a daemon thread
        web_thread = None
        if not self.args.no_web and self.cfg["web"].get("enabled", True):
            web_thread = self._start_web()

        # Open data source
        try:
            self._open_source()
        except Exception as exc:
            logger.warning("Data source unavailable: %s — dashboard only mode", exc)
            self._serial = None

        if not self.args.no_record and self._serial is not None:
            self._open_csv_writer()

        logger.info("🚀 NeuroLux main loop started — Ctrl+C to stop")

        loop_interval = 0.05  # 20 Hz main loop tick

        try:
            while self._running:
                if self._serial is None:
                    # ── auto-reconnect ──────────────────────────────
                    now = time.time()
                    if now - self._last_reconnect >= 5.0:
                        self._last_reconnect = now
                        try:
                            self._open_source()
                            self._serial_errors = 0
                            logger.info("✅ TGAM 数据源已恢复")
                        except Exception as exc:
                            logger.debug("TGAM 重连等待中: %s", exc)
                    await asyncio.sleep(1)
                    continue

                frame, raw_samples = self._next_frame()
                if frame is None:
                    await asyncio.sleep(0.01)
                    continue

                # ── signal pipeline ──
                smoothed = self.processor.process(frame, raw_samples or [])

                # Quality gate — bad signal → force LED off, skip state update
                if frame.poor_signal > 50:
                    if self.gpio_led is not None:
                        self.gpio_led.set(0)
                    continue

                # Blink detection (raw_wave samples)
                if frame.raw_wave is not None:
                    self.blink_detector.feed(frame.raw_wave, frame.timestamp)

                # Classify
                result = self.classifier.classify(smoothed)

                # Map → light command
                cmd = self.mapper.map(result)

                # ── execute ──
                should = self.mapper.should_update(cmd)
                if should:
                    logger.info("💡 灯光更新: %s bri=%d%%", cmd.state_label, cmd.brightness)
                    if self.wiz_ctrl is not None and self.wiz_ctrl._ip:
                        try:
                            await self.wiz_ctrl.fade_to(
                                brightness=cmd.brightness,
                                color_temp=cmd.color_temp,
                                duration_s=cmd.transition_s,
                            )
                        except Exception as exc:
                            logger.error("WiZ error: %s", exc)

                    if self.gpio_led is not None:
                        logger.info("→ gpio_led.set(%d)", cmd.brightness)
                        self.gpio_led.set(cmd.brightness)
                    else:
                        logger.warning("gpio_led is None — 跳过 LED")

                    self.mapper._last_cmd = cmd

                # ── broadcast ──
                if self.tcp_server is not None:
                    self.tcp_server.broadcast_brain_state(
                        attention=smoothed.attention,
                        meditation=smoothed.meditation,
                        alpha_power=smoothed.alpha_power,
                        poor_signal=frame.poor_signal,
                        eyes_state=result.state.value,
                        blink_count=self.blink_detector.blink_count,
                        eeg_bands=smoothed.eeg_bands if smoothed.eeg_bands else None,
                    )

                # ── update web dashboard state ──
                self.dash_state.update_brain(
                    attention=smoothed.attention,
                    meditation=smoothed.meditation,
                    alpha_power=smoothed.alpha_power,
                    poor_signal=frame.poor_signal,
                    eyes_state=result.state.value,
                    blink_count=self.blink_detector.blink_count,
                    eeg_bands=smoothed.eeg_bands if smoothed.eeg_bands else None,
                )
                self.dash_state.update_light(
                    brightness=cmd.brightness,
                    color_temp=cmd.color_temp,
                    state_label=cmd.state_label,
                )

                # ── CSV recording ──
                if self._csv_writer is not None:
                    write_frame_row(self._csv_writer, frame)

                # ── handle manual override (from web) ──
                if _pending_override.get("pending"):
                    bri = _pending_override.get("brightness")
                    ctemp = _pending_override.get("color_temp")
                    if self.wiz_ctrl is not None and bri is not None:
                        await self.wiz_ctrl.set_state(brightness=bri, color_temp=ctemp or 4200)
                    _pending_override["pending"] = False

                await asyncio.sleep(loop_interval)

        except KeyboardInterrupt:
            logger.info("Shutdown signal received")
        finally:
            await self._shutdown(web_thread)

    def _next_frame(self):
        """Get next BrainFrame from the active data source.

        Returns (last_frame, raw_wave_samples) tuple, or (None, []) on no data.
        raw_wave_samples collects ALL raw_wave values from this batch for
        accurate alpha-power computation.
        """
        if self._replayer is not None:
            try:
                return (next(self._replayer), [])
            except StopIteration:
                self._running = False
                return (None, [])
        else:
            try:
                data = self._serial.read(self._serial.in_waiting or 64)
                if data:
                    self._serial_errors = 0
                    frames = self.parser.feed(data)
                    if not frames:
                        return (None, [])
                    # Collect all raw_wave samples for alpha computation
                    raw_samples = [f.raw_wave for f in frames
                                   if f.raw_wave is not None]
                    return (frames[-1], raw_samples)
                return (None, [])
            except Exception as exc:
                self._serial_errors += 1
                logger.debug("Serial read error (%d): %s", self._serial_errors, exc)
                if self._serial_errors > 20:
                    logger.warning("TGAM 数据源异常 (%d 次连续错误)，触发重连", self._serial_errors)
                    try:
                        self._serial.close()
                    except Exception:
                        pass
                    self._serial = None
                    self._serial_errors = 0
            return (None, [])

    def _start_web(self) -> threading.Thread:
        """Launch Flask in a daemon thread."""
        web_cfg = self.cfg["web"]
        app = create_app(self.dash_state)

        def _serve():
            app.run(
                host=web_cfg["host"],
                port=web_cfg["port"],
                debug=False,
                use_reloader=False,
            )

        t = threading.Thread(target=_serve, daemon=True, name="flask-web")
        t.start()
        logger.info("Web dashboard → http://%s:%d", web_cfg["host"], web_cfg["port"])
        return t

    async def _shutdown(self, web_thread: Optional[threading.Thread]) -> None:
        """Graceful shutdown."""
        self._running = False

        if self.tcp_server is not None:
            await self.tcp_server.stop()

        if self.wiz_ctrl is not None:
            try:
                await self.wiz_ctrl.close()
            except Exception as exc:
                logger.debug("WiZ close error: %s", exc)

        if self.gpio_led is not None:
            self.gpio_led.cleanup()

        if self._csv_fh is not None:
            self._csv_fh.close()

        if self._replayer is None and hasattr(self, "_serial"):
            try:
                self._serial.close()
            except Exception:
                pass

        logger.info("NeuroLux shutdown complete.")


# ═══════════════════════════════════════════════════════════════════════
#  calibrate mode (standalone)
# ═══════════════════════════════════════════════════════════════════════


def run_calibrate() -> None:
    """Interactive personal alpha-baseline calibration."""
    print("\n" + "=" * 58)
    print("  🧠  NeuroLux — 个人 α 基线校准")
    print("=" * 58)
    print()
    print("  校准流程：")
    print("    1. 戴好 TGAM，确认蓝牙已连接")
    print("    2. 听到提示后睁眼看屏幕 10 秒")
    print("    3. 听到提示后闭眼放松 10 秒")
    print("    4. 自动计算你的 α 阈值并写入 config.yaml")
    print()

    input("  按 Enter 开始…")

    import serial as pyserial

    try:
        ser = pyserial.Serial("/dev/rfcomm0", 57600, timeout=0.5)
    except Exception as exc:
        print(f"\n  ❌ 无法打开串口: {exc}")
        return

    parser = ThinkGearParser()
    processor = SignalProcessor(attn_window=5, alpha_window=10)

    def _collect(duration_s: float, label: str) -> list[float]:
        print(f"\n  ▶ {label} ({duration_s}s) …", end="", flush=True)
        alphas: list[float] = []
        deadline = time.time() + duration_s
        while time.time() < deadline:
            data = ser.read(ser.in_waiting or 64)
            for frame in parser.feed(data):
                smoothed = processor.process(frame)
                if smoothed.alpha_power > 0:
                    alphas.append(smoothed.alpha_power)
            time.sleep(0.05)
        print(f"  收集 {len(alphas)} 帧")
        return alphas

    eyes_open_alphas = _collect(10, "睁眼看屏幕")
    eyes_closed_alphas = _collect(10, "闭眼放松")

    ser.close()

    if not eyes_open_alphas or not eyes_closed_alphas:
        print("\n  ❌ 未收集到有效数据 — 请检查 TGAM 连接")
        return

    open_mean = sum(eyes_open_alphas) / len(eyes_open_alphas)
    closed_mean = sum(eyes_closed_alphas) / len(eyes_closed_alphas)

    # Schmitt thresholds: close-thresh = 70% between open/closed mean
    #                    open-thresh  = 40% between open/closed mean
    range_ = closed_mean - open_mean
    alpha_close = open_mean + range_ * 0.70
    alpha_open = open_mean + range_ * 0.30

    print(f"\n  ┌─────────────────────────────────────┐")
    print(f"  │  睁眼平均 α:  {open_mean:8.1f}              │")
    print(f"  │  闭眼平均 α:  {closed_mean:8.1f}              │")
    print(f"  │  建议 CLOSED 阈值: {alpha_close:8.1f}          │")
    print(f"  │  建议 OPEN  阈值: {alpha_open:8.1f}          │")
    print(f"  └─────────────────────────────────────┘")

    # Write to config.yaml
    config_path = PROJECT_ROOT / "config.yaml"
    try:
        import yaml
        cfg = {}
        if config_path.exists():
            with open(config_path) as fh:
                cfg = yaml.safe_load(fh) or {}
        cfg.setdefault("signal", {})
        cfg["signal"]["alpha_close_thresh"] = round(alpha_close, 1)
        cfg["signal"]["alpha_open_thresh"] = round(alpha_open, 1)
        with open(config_path, "w") as fh:
            yaml.dump(cfg, fh, default_flow_style=False, allow_unicode=True)
        print(f"\n  ✅ 阈值已写入 {config_path}")
    except ImportError:
        print("\n  ⚠️  PyYAML 未安装 — 请手动更新 config.yaml:")
        print(f"      signal.alpha_close_thresh: {alpha_close:.1f}")
        print(f"      signal.alpha_open_thresh:  {alpha_open:.1f}")


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="驭光 NeuroLux — 脑波驱灯 · 意念驭光",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 main.py                              # live TGAM → full pipeline
  python3 main.py --replay data/session.csv    # offline CSV playback
  python3 main.py --no-light                   # brain analysis only
  python3 main.py --no-web --no-tcp            # minimal: just control light
  python3 main.py --calibrate                  # personal alpha calibration
  python3 main.py --config prod.yaml           # use custom config
        """,
    )
    ap.add_argument("--config", help="Path to YAML config file")
    ap.add_argument("--replay", help="CSV file for offline replay")
    ap.add_argument("--calibrate", action="store_true", help="Run alpha baseline calibration")
    ap.add_argument("--no-light", action="store_true", help="Disable light control")
    ap.add_argument("--no-tcp", action="store_true", help="Disable TCP broadcast")
    ap.add_argument("--no-web", action="store_true", help="Disable web dashboard")
    ap.add_argument("--no-record", action="store_true", help="Don't save CSV session")
    ap.add_argument("--simulate-gpio", action="store_true", help="Simulate GPIO (no hardware)")
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    if args.calibrate:
        run_calibrate()
        return

    app = NeuroLux(args)
    app.run()


if __name__ == "__main__":
    main()
