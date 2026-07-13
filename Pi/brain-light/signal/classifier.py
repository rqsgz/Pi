#!/usr/bin/env python3
"""Brain-state classifier — maps smoothed EEG features to discrete eye states.

Based on the α-blocking phenomenon:
    - Closed eyes → α power surges   (idling visual cortex)
    - Open eyes   → α power drops    (visual input suppresses α)

Decision rule (Schmitt-trigger hysteresis):
    CLOSED:  alpha > 150   AND  attention < 45
    OPEN:    alpha < 60    OR   attention > 55
    Otherwise → TRANSITION (keep previous state briefly)

Anti-flutter protections:
    - State-change must persist for `confirm_time` seconds
    - After a confirmed switch, a `cooldown` prevents another change
    - Long-closed (>60 s) → "sleep" sub-state (lights dim to off)
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


class EyesState(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    TRANSITION = "transition"
    LONG_CLOSED = "long_closed"   # >60 s closed → sleep mode


@dataclass
class ClassifierResult:
    timestamp: float
    state: EyesState
    alpha_power: float
    attention: float
    meditation: float
    closed_duration_s: float = 0.0    # how long eyes have been closed


class StateClassifier:
    """Schmitt-trigger state classifier for eye-open / eye-closed detection.

    Parameters
    ----------
    alpha_close_thresh : float
        Alpha power above this → consider CLOSED (default 150).
    alpha_open_thresh : float
        Alpha power below this (OR high attention) → consider OPEN (default 60).
    attn_close_thresh : float
        Attention below this required for CLOSED (default 45).
    attn_open_thresh : float
        Attention above this → force OPEN regardless of alpha (default 55).
    confirm_time : float
        New state must persist this many seconds before switching (default 1.0).
    cooldown : float
        After a state change, ignore new changes for this long (default 3.0).
    sleep_time : float
        Continuous CLOSED beyond this → LONG_CLOSED sleep mode (default 60.0).
    """

    def __init__(
        self,
        alpha_close_thresh: float = 150.0,
        alpha_open_thresh: float = 60.0,
        attn_close_thresh: float = 45.0,
        attn_open_thresh: float = 55.0,
        confirm_time: float = 1.0,
        cooldown: float = 3.0,
        sleep_time: float = 60.0,
        inverted: bool = False,
    ) -> None:
        # Schmitt-trigger thresholds
        self.alpha_close = alpha_close_thresh
        self.alpha_open = alpha_open_thresh
        self.attn_close = attn_close_thresh
        self.attn_open = attn_open_thresh

        # Inverted mode: for users whose alpha DROPS when eyes close
        self.inverted = inverted

        # Timing
        self.confirm_time = confirm_time
        self.cooldown = cooldown
        self.sleep_time = sleep_time

        # Internal state
        self._state: EyesState = EyesState.OPEN
        self._pending_state: Optional[EyesState] = None
        self._pending_since: float = 0.0
        self._last_switch_time: float = 0.0
        self._closed_since: float = 0.0   # wall time when CLOSED first detected

    # ── properties ──────────────────────────────────────────────────────

    @property
    def state(self) -> EyesState:
        return self._state

    @property
    def closed_duration(self) -> float:
        """Seconds spent in CLOSED (continuous).  0 if not currently closed."""
        if self._state in (EyesState.CLOSED, EyesState.LONG_CLOSED):
            return time.time() - self._closed_since
        return 0.0

    # ── classify ────────────────────────────────────────────────────────

    def classify(self, smoothed) -> ClassifierResult:
        """Classify one smoothed frame.

        Parameters
        ----------
        smoothed : SmoothedFrame
            Output of SignalProcessor.process().

        Returns
        -------
        ClassifierResult
        """
        now = time.time()
        alpha = smoothed.alpha_power
        attn = smoothed.attention

        # ── raw decision (instantaneous) ──
        if self.inverted:
            # Inverted alpha: closed eyes → alpha DROPS
            if alpha < self.alpha_close and attn < self.attn_close:
                raw = EyesState.CLOSED
            elif alpha > self.alpha_open or attn > self.attn_open:
                raw = EyesState.OPEN
            else:
                raw = EyesState.TRANSITION
        else:
            # Normal alpha blocking: closed eyes → alpha RISES
            if alpha > self.alpha_close and attn < self.attn_close:
                raw = EyesState.CLOSED
            elif alpha < self.alpha_open or attn > self.attn_open:
                raw = EyesState.OPEN
            else:
                raw = EyesState.TRANSITION

        # ── debounce: pending state must persist ──
        if raw != self._state:
            if self._pending_state == raw:
                # Same pending — has enough time elapsed?
                if now - self._pending_since >= self.confirm_time:
                    # Also check cooldown since last confirmed switch
                    if now - self._last_switch_time >= self.cooldown:
                        self._commit_state(raw, now, alpha)
            else:
                self._pending_state = raw
                self._pending_since = now
        else:
            # Back to current state → cancel pending
            self._pending_state = None

        # ── LONG_CLOSED detection ──
        if self._state == EyesState.CLOSED and self.closed_duration >= self.sleep_time:
            self._commit_state(EyesState.LONG_CLOSED, now, alpha)

        return ClassifierResult(
            timestamp=now,
            state=self._state,
            alpha_power=alpha,
            attention=attn,
            meditation=smoothed.meditation,
            closed_duration_s=self.closed_duration,
        )

    # ── helpers ─────────────────────────────────────────────────────────

    def _commit_state(self, new_state: EyesState, now: float,
                      alpha_power: float = 0.0) -> None:
        old = self._state
        self._state = new_state
        self._pending_state = None
        self._last_switch_time = now

        if new_state in (EyesState.CLOSED, EyesState.LONG_CLOSED):
            if old not in (EyesState.CLOSED, EyesState.LONG_CLOSED):
                self._closed_since = now
        else:
            self._closed_since = 0.0

        logger.info("State: %s → %s  (α=%.0f)", old.value, new_state.value,
                    alpha_power)
