#!/usr/bin/env python3
"""WiZ smart-light controller — wraps the pywizlight library.

Philips WiZ bulbs are controlled via UDP port 38899.  The ``pywizlight``
library handles discovery, state building, and async communication.

Usage
-----
    ctrl = WiZController("192.168.1.11")
    await ctrl.turn_on(brightness=80, color_temp=4200, speed=10)

    # Or with fade_to:
    await ctrl.fade_to(brightness=20, color_temp=2400, duration_s=2.0)

    # Discovery:
    bulbs = ctrl.discover()          # → dict[MAC → WiZState]
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from pywizlight import PilotBuilder, wizlight
from pywizlight.discovery import discover_lights

logger = logging.getLogger(__name__)

UDP_PORT = 38899


@dataclass
class WiZState:
    """Snapshot of a WiZ bulb's current state."""

    ip: str = ""
    mac: str = ""
    is_on: bool = False
    brightness: int = 100      # 1–100 %
    color_temp: int = 4200     # 2200–6500 K
    r: int = 0
    g: int = 0
    b: int = 0
    scene_id: int = 0
    raw: dict = field(default_factory=dict)


def _transition_s_to_speed(duration_s: float) -> int:
    """Convert fade duration (seconds) → WiZ speed (10–200, higher=faster).

    pywizlight validates speed 10-200.  Rough WiZ protocol mapping:
        speed 200 → ~0.1 s  fade (instant)
        speed 120 → ~1   s  fade
        speed  60 → ~2   s  fade
        speed  25 → ~5   s  fade
        speed  10 → ~10  s  fade  (slowest)
    """
    if duration_s <= 0:
        return 200
    speed = int(120.0 / duration_s)
    return max(10, min(200, speed))


class WiZController:
    """Control a Philips WiZ smart bulb via pywizlight.

    Parameters
    ----------
    bulb_ip : str | None
        IP address of the bulb.  If None, call discover() first.
    port : int
        UDP port (default 38899).  Kept for API compatibility.
    timeout : float
        Socket timeout in seconds (kept for API compatibility).
    """

    def __init__(
        self,
        bulb_ip: Optional[str] = None,
        port: int = UDP_PORT,
        timeout: float = 5.0,
    ) -> None:
        self._ip: Optional[str] = bulb_ip
        self._port = port
        self._timeout = timeout
        self._bulb: Optional[wizlight] = None

    # ── properties ───────────────────────────────────────────────────

    @property
    def bulb_ip(self) -> Optional[str]:
        return self._ip

    @property
    def bulb(self) -> Optional[wizlight]:
        """The underlying pywizlight wizlight instance (or None)."""
        return self._bulb

    # ── discovery ────────────────────────────────────────────────────

    def discover(self, timeout: float = 5.0) -> dict[str, WiZState]:
        """Broadcast discovery — find all WiZ bulbs on the LAN.

        Returns a dict mapping MAC → WiZState.
        """
        bulbs_found = discover_lights(
            broadcast_space="255.255.255.255",
            wait_time=timeout,
        )

        result: dict[str, WiZState] = {}
        for bulb in bulbs_found:
            mac = getattr(bulb, "mac", bulb.ip)
            result[mac] = WiZState(
                ip=bulb.ip,
                mac=mac,
            )
            logger.info("Discovered WiZ bulb: %s @ %s", mac, bulb.ip)

        if not result:
            logger.warning("No WiZ bulbs discovered — check LAN connectivity")
        return result

    # ── helpers ──────────────────────────────────────────────────────

    def _get_bulb(self) -> wizlight:
        """Return the wizlight instance, creating it on first use."""
        if self._bulb is None:
            if self._ip is None:
                raise RuntimeError("bulb_ip not set — call discover() first")
            self._bulb = wizlight(self._ip)
        return self._bulb

    async def _call(self, coro, label: str = "WiZ"):
        """Wrap a pywizlight coroutine with a timeout.  Returns result or None on timeout."""
        try:
            return await asyncio.wait_for(coro, timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning("%s 超时 (%.0fs) — 灯泡可能离线", label, self._timeout)
            return None

    # ── basic control ────────────────────────────────────────────────

    async def turn_on(
        self,
        brightness: Optional[int] = None,
        color_temp: Optional[int] = None,
        speed: Optional[int] = None,
    ) -> None:
        """Turn the bulb on, optionally with brightness / colour temp / speed.

        Parameters
        ----------
        brightness : int | None
            10–100 %.
        color_temp : int | None
            2200–6500 Kelvin.
        speed : int | None
            Fade speed 1–100 (higher = faster).
        """
        # brightness ≤ 0 → turn off completely (sleep mode)
        if brightness is not None and int(brightness) <= 0:
            bulb = self._get_bulb()
            await self._call(bulb.turn_off(), "turn_off")
            return

        pb_kwargs: dict = {}
        if brightness is not None:
            pb_kwargs["brightness"] = max(10, min(100, int(brightness)))
        if color_temp is not None:
            pb_kwargs["colortemp"] = max(2200, min(6500, int(color_temp)))
        if speed is not None:
            pb_kwargs["speed"] = max(10, min(200, int(speed)))

        bulb = self._get_bulb()
        await self._call(bulb.turn_on(PilotBuilder(**pb_kwargs)), "turn_on")
        logger.debug("WiZ turn_on: %s", pb_kwargs)

    async def turn_off(self) -> None:
        """Turn the bulb off."""
        bulb = self._get_bulb()
        await self._call(bulb.turn_off(), "turn_off")

    async def set_brightness(self, brightness: int) -> None:
        """Set brightness 1–100 %."""
        b = max(10, min(100, int(brightness)))
        await self.turn_on(brightness=b)

    async def set_color_temp(self, kelvin: int) -> None:
        """Set colour temperature 2200–6500 K."""
        t = max(2200, min(6500, int(kelvin)))
        await self.turn_on(color_temp=t)

    async def set_scene(self, scene_id: int) -> None:
        """Activate a pre-saved scene by ID."""
        bulb = self._get_bulb()
        await self._call(bulb.turn_on(PilotBuilder(scene=int(scene_id))), "set_scene")

    # ── combined control ─────────────────────────────────────────────

    async def set_state(
        self,
        brightness: Optional[int] = None,
        color_temp: Optional[int] = None,
        speed: Optional[int] = None,
    ) -> None:
        """Convenience: set brightness, colour temp, and/or fade speed."""
        await self.turn_on(
            brightness=brightness,
            color_temp=color_temp,
            speed=speed,
        )

    async def fade_to(
        self,
        brightness: int,
        color_temp: int,
        duration_s: float = 1.0,
    ) -> None:
        """Smooth transition to the given brightness & colour temp.

        Parameters
        ----------
        brightness : int
            1–100 %.
        color_temp : int
            2200–6500 K.
        duration_s : float
            Desired transition duration in seconds (converted to WiZ speed).
        """
        speed = _transition_s_to_speed(duration_s)
        await self.turn_on(
            brightness=brightness,
            color_temp=color_temp,
            speed=speed,
        )

    # ── query ────────────────────────────────────────────────────────

    async def get_state(self) -> WiZState:
        """Query the bulb's current state."""
        bulb = self._get_bulb()
        pilot_list = await self._call(bulb.updateState(), "updateState")
        if not pilot_list:
            return WiZState(ip=self._ip or "")
        # updateState() returns List[PilotParser]; use first (and usually only) element
        state = pilot_list[0]
        if state is None:
            return WiZState(ip=self._ip or "")
        return WiZState(
            ip=self._ip or "",
            mac=getattr(state, "mac", ""),
            is_on=state.get_state(),
            brightness=state.get_brightness(),
            color_temp=state.get_colortemp(),
            r=getattr(state, "r", 0),
            g=getattr(state, "g", 0),
            b=getattr(state, "b", 0),
            scene_id=state.get_scene_id(),
        )

    # ── lifecycle ────────────────────────────────────────────────────

    async def close(self) -> None:
        """Release the connection."""
        if self._bulb is not None:
            await self._call(self._bulb.async_close(), "close")
            self._bulb = None

    def close_sync(self) -> None:
        """Synchronous close (for use in non-async shutdown paths)."""
        self._bulb = None


# ── CLI discovery tool ──────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def _main():
        bulb_ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.11"
        ctrl = WiZController(bulb_ip)

        if "--discover" in sys.argv:
            bulbs = ctrl.discover()
            if not bulbs:
                print("No WiZ bulbs found on the LAN.")
            else:
                for mac, st in bulbs.items():
                    print(f"\n💡 {mac}")
                    print(f"   IP: {st.ip}")
        elif "--on" in sys.argv:
            await ctrl.turn_on(brightness=80, color_temp=4200)
            print("Turned ON")
        elif "--off" in sys.argv:
            await ctrl.turn_off()
            print("Turned OFF")
        elif "--state" in sys.argv:
            st = await ctrl.get_state()
            print(
                f"ON={st.is_on}  brightness={st.brightness}%  "
                f"temp={st.color_temp}K"
            )
        else:
            print("Usage: python wiz_controller.py <IP> --on | --off | --state | --discover")

        await ctrl.close()

    asyncio.run(_main())
