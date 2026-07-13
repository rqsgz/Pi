#!/usr/bin/env python3
"""Blink detector — identifies eye-blink spikes in the raw EEG waveform.

Eye blinks produce large-amplitude, short-duration spikes (50–300 ms)
in the raw-wave signal (512 Hz).  This detector uses a rolling standard-
deviation window and a >3σ threshold to flag individual blinks, plus
double-blink detection (two blinks within 1 second).

Blink events can serve as an extra control channel — e.g. double-blink
to toggle between lighting presets.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BlinkEvent:
    timestamp: float
    amplitude: float       # peak |raw_value| during blink
    duration_ms: float     # width of the spike
    is_double: bool = False


class BlinkDetector:
    """Real-time eye-blink spike detector.

    Parameters
    ----------
    sample_rate : int
        TGAM raw-wave sample rate (512 Hz).
    window_ms : int
        Rolling window for std computation (default 500 ms).
    sigma_thresh : float
        Number of standard deviations above mean to flag (default 3.0).
    min_width_ms : int
        Minimum spike width to reject noise (default 50 ms).
    max_width_ms : int
        Maximum spike width to reject muscle artefacts (default 300 ms).
    double_window_s : float
        Two blinks within this window count as a double-blink (default 1.0 s).
    """

    def __init__(
        self,
        sample_rate: int = 512,
        window_ms: int = 500,
        sigma_thresh: float = 3.0,
        min_width_ms: int = 50,
        max_width_ms: int = 300,
        double_window_s: float = 1.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.window_samples = int(sample_rate * window_ms / 1000)
        self.sigma_thresh = sigma_thresh
        self.min_width = int(sample_rate * min_width_ms / 1000)
        self.max_width = int(sample_rate * max_width_ms / 1000)
        self.double_window_s = double_window_s

        # Rolling buffer for baseline statistics
        self._buffer: deque[int] = deque(maxlen=self.window_samples)
        self._baseline_mean: float = 0.0
        self._baseline_std: float = 1.0

        # Spike detection state machine
        self._in_spike = False
        self._spike_start_idx: int = 0
        self._spike_samples: list[int] = []
        self._sample_idx: int = 0

        # Blink history for double-blink detection
        self._last_blink_ts: float = 0.0
        self._blink_count: int = 0
        self._pending_double: bool = False

    # ── public ─────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all internal buffers and detection state."""
        self._buffer.clear()
        self._baseline_mean = 0.0
        self._baseline_std = 1.0
        self._in_spike = False
        self._spike_start_idx = 0
        self._spike_samples.clear()
        self._sample_idx = 0
        self._last_blink_ts = 0.0
        self._pending_double = False

    @property
    def blink_count(self) -> int:
        return self._blink_count

    @property
    def baseline(self) -> tuple[float, float]:
        """Return (mean, std) of the current rolling window."""
        return self._baseline_mean, self._baseline_std

    def feed(self, raw_wave: int, timestamp: float) -> Optional[BlinkEvent]:
        """Push one raw-wave sample; returns a BlinkEvent on detection.

        Returns None on most calls.  A non-None return signals that a
        complete blink spike was just recognised.
        """
        self._sample_idx += 1
        self._buffer.append(raw_wave)

        # Recompute baseline
        if len(self._buffer) >= 10:
            self._baseline_mean = sum(self._buffer) / len(self._buffer)
            var = sum((x - self._baseline_mean) ** 2 for x in self._buffer) / len(self._buffer)
            self._baseline_std = max(var ** 0.5, 1.0)

        # Spike detection
        deviation = abs(raw_wave - self._baseline_mean)
        threshold = self.sigma_thresh * self._baseline_std

        if not self._in_spike:
            if deviation > threshold:
                self._in_spike = True
                self._spike_start_idx = self._sample_idx
                self._spike_samples = [raw_wave]
        else:
            self._spike_samples.append(raw_wave)
            if deviation <= threshold * 0.5:  # fell below half-threshold → end spike
                self._in_spike = False
                event = self._finalise_spike(timestamp)
                if event is not None:
                    return event

        return None

    def _finalise_spike(self, timestamp: float) -> Optional[BlinkEvent]:
        """Check whether the collected spike looks like a blink."""
        width = len(self._spike_samples)
        if not (self.min_width <= width <= self.max_width):
            self._spike_samples.clear()
            return None

        amplitude = max(abs(v) for v in self._spike_samples)
        duration_ms = width / self.sample_rate * 1000

        # Double-blink check
        is_double = False
        if self._last_blink_ts > 0 and (timestamp - self._last_blink_ts) < self.double_window_s:
            is_double = True

        self._last_blink_ts = timestamp
        self._blink_count += 1
        self._spike_samples.clear()

        return BlinkEvent(
            timestamp=timestamp,
            amplitude=amplitude,
            duration_ms=duration_ms,
            is_double=is_double,
        )
