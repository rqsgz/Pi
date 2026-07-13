#!/usr/bin/env python3
"""Brain-state → light-parameter mapper.

Translates the discrete eyes-state from the classifier into concrete
(colour-temperature, brightness, transition-duration) tuples that the
WiZ controller (or GPIO LED) can consume directly.

Mapping table (5 states)
-------------------------
    OPEN + focused   → 5000 K  85 %  cold white  (reading/screen)
    OPEN + relaxed   → 4200 K  65 %  natural white
    TRANSITION       → 3500 K  40 %  warm amber
    CLOSED           → 2400 K  20 %  warm orange  (relaxation)
    LONG_CLOSED      → 2200 K   3 %→ off (sunset→sleep)

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
    color_temp: int          # 1700–6500 K
    transition_s: float      # fade duration
    state_label: str         # human-readable label


# ── default five-state mapping table ────────────────────────────────────

_MAPPING_TABLE: dict[str, LightCommand] = {
    # EyesState value → (brightness, color_temp, transition, label)
    "open_focused": LightCommand(
        brightness=85, color_temp=5000, transition_s=0.8, state_label="睁眼·专注"
    ),
    "open_relaxed": LightCommand(
        brightness=65, color_temp=4200, transition_s=1.0, state_label="睁眼·普通"
    ),
    "transition": LightCommand(
        brightness=40, color_temp=3500, transition_s=1.5, state_label="过渡状态"
    ),
    "closed": LightCommand(
        brightness=20, color_temp=2400, transition_s=2.0, state_label="闭眼·放松"
    ),
    "long_closed": LightCommand(
        brightness=0, color_temp=2200, transition_s=5.0, state_label="长闭·入睡"
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

        Compares brightness and color_temp against the last issued command.
        """
        if self._last_cmd is None:
            return True
        return (
            new_cmd.brightness != self._last_cmd.brightness
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
            "💡 %s  temp=%dK  bri=%d%%  fade=%.1fs",
            cmd.state_label,
            cmd.color_temp,
            cmd.brightness,
            cmd.transition_s,
        )

        # WiZ bulb
        if wiz_ctrl is not None:
            try:
                wiz_ctrl.fade_to(
                    brightness=cmd.brightness,
                    color_temp=cmd.color_temp,
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
