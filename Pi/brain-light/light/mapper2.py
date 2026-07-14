#!/usr/bin/env python3
"""Brain-state → light-parameter mapper.

Translates the discrete eyes-state from the classifier into concrete
(RGB colour, brightness, transition-duration) tuples that the
WiZ controller (or GPIO LED) can consume directly.

Mapping table (5 states)
-------------------------
    OPEN + focused   → Red   (255,0,0)   85 %  (reading/screen)
    OPEN + relaxed   → Red   (255,30,0)  65 %  (natural)
    TRANSITION       → Green (0,255,0)   40 %  (blink/transition)
    CLOSED           → Blue  (0,0,255)   20 %  (relaxation)
    LONG_CLOSED      → Dim   (0,0,30)     0 %→ off (sunset→sleep)

Cooldown between state changes prevents rapid flickering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from signal.classifier import EyesState

logger = logging.getLogger(__name__)


@dataclass
class LightCommand:
    """Desired light state produced by the mapper."""

    brightness: int          # 0–100
    color_temp: int = 4200   # 1700–6500 K (unused when rgb is set)
    rgb: tuple | None = None # (R, G, B) 0-255, overrides color_temp when set
    transition_s: float = 1.0  # fade duration
    state_label: str = ""    # human-readable label


# ── default five-state mapping table ────────────────────────────────────

_MAPPING_TABLE: dict[str, LightCommand] = {
    # EyesState → (brightness, rgb, transition, label)
    # 睁眼→红  过渡→绿  闭眼→蓝
    "open_focused": LightCommand(
        brightness=85, rgb=(255, 0, 0), transition_s=0.8, state_label="睁眼·专注"
    ),
    "open_relaxed": LightCommand(
        brightness=65, rgb=(255, 30, 0), transition_s=1.0, state_label="睁眼·普通"
    ),
    "transition": LightCommand(
        brightness=40, rgb=(0, 255, 0), transition_s=1.5, state_label="过渡状态"
    ),
    "closed": LightCommand(
        brightness=20, rgb=(0, 0, 255), transition_s=2.0, state_label="闭眼·放松"
    ),
    "long_closed": LightCommand(
        brightness=0, rgb=(0, 0, 30), transition_s=5.0, state_label="长闭·入睡"
    ),
}


class LightMapper:
    """Map classifier output to light parameters.

    Parameters
    ----------
    mapping : dict | None
        Override the default mapping table.
    attn_focus_thresh : float
        Attention above this → "focused" sub-state (default 60).
    """

    def __init__(
        self,
        mapping: Optional[dict[str, LightCommand]] = None,
        attn_focus_thresh: float = 60.0,
    ) -> None:
        self._table = mapping or dict(_MAPPING_TABLE)
        self._attn_focus_thresh = attn_focus_thresh
        self._last_cmd: Optional[LightCommand] = None

    # ── properties ──────────────────────────────────────────────────

    @property
    def last_command(self) -> Optional[LightCommand]:
        return self._last_cmd

    # ── map ──────────────────────────────────────────────────────────

    def map(self, result) -> LightCommand:
        """Convert a classifier result into a light command.

        Parameters
        ----------
        result : ClassifierResult
            From StateClassifier.classify().

        Returns
        -------
        LightCommand
        """
        state = result.state

        if state == EyesState.OPEN:
            if result.attention >= self._attn_focus_thresh:
                key = "open_focused"
            else:
                key = "open_relaxed"
        elif state == EyesState.CLOSED:
            key = "closed"
        elif state == EyesState.LONG_CLOSED:
            key = "long_closed"
        else:
            key = "transition"

        cmd = self._table.get(key, self._table["open_relaxed"])
        return cmd

    def should_update(self, new_cmd: LightCommand) -> bool:
        """Return True if the light needs updating (avoids duplicate UDP calls).

        Compares brightness, rgb, and color_temp against the last issued command.
        """
        if self._last_cmd is None:
            return True
        return (
            new_cmd.brightness != self._last_cmd.brightness
            or new_cmd.rgb != self._last_cmd.rgb
            or new_cmd.color_temp != self._last_cmd.color_temp
        )

    def apply(self, result, wiz_ctrl, gpio_led=None) -> LightCommand:
        """Full map + apply pipeline — classify → map → send to WiZ + GPIO.

        Parameters
        ----------
        result : ClassifierResult
        wiz_ctrl : WiZController
        gpio_led : GpioLed | None

        Returns
        -------
        LightCommand
        """
        cmd = self.map(result)
        if not self.should_update(cmd):
            return cmd

        logger.info(
            "💡 %s  rgb=%s  bri=%d%%  fade=%.1fs",
            cmd.state_label,
            cmd.rgb,
            cmd.brightness,
            cmd.transition_s,
        )

        # WiZ bulb
        if wiz_ctrl is not None:
            try:
                wiz_ctrl.fade_to(
                    brightness=cmd.brightness,
                    color_temp=cmd.color_temp,
                    rgb=cmd.rgb,
                    duration_s=cmd.transition_s,
                )
            except Exception as exc:
                logger.error("WiZ command failed: %s", exc)

        # GPIO LED (brightness-only proxy)
        if gpio_led is not None:
            try:
                gpio_led.fade_to(cmd.brightness, duration=cmd.transition_s)
            except Exception as exc:
                logger.error("GPIO LED command failed: %s", exc)

        self._last_cmd = cmd
        return cmd
