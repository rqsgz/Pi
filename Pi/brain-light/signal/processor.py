#!/usr/bin/env python3
"""Signal processor — sliding-window smoothing & alpha-power extraction.

Pipeline stage 3 (after parser, before classifier):

    raw frames → [Processor] → smoothed features
                   │
                   ├─ attention   10-frame (~1 s) moving average
                   ├─ meditation  10-frame (~1 s) moving average
                   └─ alpha_power 20-frame (~2 s) moving average
                      (alpha = low_alpha + high_alpha)
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SmoothedFrame:
    """One frame after sliding-window smoothing."""

    timestamp: float = 0.0
    poor_signal: int = 0
    attention: float = 0.0           # smoothed 0–100
    meditation: float = 0.0           # smoothed 0–100
    raw_wave: Optional[int] = None
    alpha_power: float = 0.0          # low_α + high_α, smoothed
    eeg_bands: dict[str, float] = field(default_factory=dict)


class SignalProcessor:
    """Apply moving-average smoothing to selected TGAM features.

    Attention/meditation use a simple sliding-window mean.
    Alpha power uses an Exponential Moving Average (EMA) with outlier
    rejection to suppress the biquad filter's inherent spike noise
    while keeping latency low.

    Parameters
    ----------
    attn_window : int
        Number of frames for attention/meditation smoothing (default 10).
    alpha_window : int
        Number of frames for alpha-power smoothing (default 40 — used
        as EMA effective window reference; actual EMA time-constant is
        alpha_window / 2).
    """

    def __init__(
        self,
        attn_window: int = 10,
        alpha_window: int = 40,
    ) -> None:
        self._attn_window = attn_window
        self._alpha_window = alpha_window
        self._attn_buf: deque[float] = deque(maxlen=attn_window)
        self._med_buf: deque[float] = deque(maxlen=attn_window)

        # EMA for alpha — much smoother than SMA, less lag
        self._alpha_ema: float = 0.0
        self._alpha_ema_init: bool = False
        # EMA coefficient: 2/(window+1)  → ~40-sample effective window
        self._alpha_smooth = 2.0 / (max(alpha_window, 4) + 1)

        # Running statistics for outlier rejection
        self._alpha_m2: float = 0.0   # running variance helper (Welford)

        # ── stale-data guard ──────────────────────────────────────────
        self._last_alpha_raw: float = -1.0
        self._alpha_repeat_count: int = 0
        self._stale_warned: bool = False

    def reset(self) -> None:
        """Clear all internal buffers and state.

        Call this on TGAM reconnect / data-source switch to prevent
        stale old values from polluting the smoothed output.
        """
        self._attn_buf.clear()
        self._med_buf.clear()
        self._alpha_ema = 0.0
        self._alpha_ema_init = False
        self._alpha_m2 = 0.0
        self._last_alpha_raw = -1.0
        self._alpha_repeat_count = 0
        self._stale_warned = False
        # Also reset biquad state if it exists
        for attr in ('_bq_z1', '_bq_z2'):
            if hasattr(self, attr):
                delattr(self, attr)
        logger.info("SignalProcessor reset — buffers cleared")

    @property
    def is_stale(self) -> bool:
        """True if the alpha input hasn't changed for many consecutive frames."""
        return self._alpha_repeat_count > 40  # ~4 s at 10 Hz

    def process(self, frame, raw_wave_samples=None) -> SmoothedFrame:
        """Push one raw BrainFrame through the smoothing pipeline.

        Parameters
        ----------
        frame : BrainFrame
            Parsed frame from tgam.parser.
        raw_wave_samples : list[int] | None
            All raw_wave values from this batch, for alpha extraction.

        Returns
        -------
        SmoothedFrame
        """
        import math

        attn = float(frame.attention) if frame.attention is not None else 0.0
        med = float(frame.meditation) if frame.meditation is not None else 0.0

        # Compute instantaneous alpha power from EEG power bands
        alpha_instant = 0.0
        eeg_bands: dict[str, float] = {}
        if frame.eeg_power:
            low = frame.eeg_power.get("low_alpha", 0)
            high = frame.eeg_power.get("high_alpha", 0)
            alpha_instant = float(low + high)
            eeg_bands = {k: float(v) for k, v in frame.eeg_power.items()}

        # Fallback: compute alpha power from raw_wave samples
        if alpha_instant == 0.0 and raw_wave_samples:
            alpha_instant = self._compute_alpha_from_raw(raw_wave_samples)
            # Synthesise proxy eeg_bands so radar chart has data
            half_alpha = alpha_instant / 2.0
            eeg_bands = {
                "delta": alpha_instant * 0.3,
                "theta": alpha_instant * 0.5,
                "low_alpha": half_alpha,
                "high_alpha": half_alpha,
                "low_beta": alpha_instant * 0.4,
                "high_beta": alpha_instant * 0.2,
                "low_gamma": alpha_instant * 0.1,
                "mid_gamma": alpha_instant * 0.05,
            }

        # Push into sliding buffers (attn/med use SMA)
        self._attn_buf.append(attn)
        self._med_buf.append(med)

        # ── stale-data detection ──────────────────────────────────
        if alpha_instant == self._last_alpha_raw and alpha_instant > 0:
            self._alpha_repeat_count += 1
        else:
            self._alpha_repeat_count = 0
        self._last_alpha_raw = alpha_instant

        # ── alpha: EMA with adaptive outlier weighting ──────────
        if not self._alpha_ema_init:
            self._alpha_ema = alpha_instant
            self._alpha_ema_init = True
        else:
            # ── cliff detection: EMA cliffed at huge value but real
            #     alpha dropped to near-zero (e.g. TGAM removed from head).
            #     Fast-forward the EMA instead of waiting 4+ minutes.
            if self._alpha_ema > 1000 and alpha_instant < self._alpha_ema * 0.1:
                logger.info(
                    "Alpha cliff detected: EMA=%.0f → instant=%.0f — fast-forwarding",
                    self._alpha_ema, alpha_instant,
                )
                self._alpha_ema = alpha_instant
                self._alpha_m2 = 0.0
            else:
                # Compute weight based on how "surprising" the new value is.
                std = self._alpha_m2 ** 0.5 if self._alpha_m2 > 0 else self._alpha_ema * 0.3
                max_jump = max(std * 4.0, self._alpha_ema * 1.5)
                deviation = abs(alpha_instant - self._alpha_ema)

                if deviation <= max_jump:
                    weight = 1.0           # normal — full update
                elif deviation <= max_jump * 2:
                    weight = 0.4           # mild surprise — partial update
                else:
                    weight = 0.08          # large spike — barely move

                effective_smooth = self._alpha_smooth * weight
                self._alpha_ema += effective_smooth * (alpha_instant - self._alpha_ema)

                # Welford running variance
                delta = alpha_instant - self._alpha_ema
                self._alpha_m2 += effective_smooth * (delta * delta - self._alpha_m2)

        smoothed_alpha = self._alpha_ema

        # Compute means (avoid NaN for empty buffers)
        smoothed_attn = sum(self._attn_buf) / len(self._attn_buf) if self._attn_buf else 0.0
        smoothed_med = sum(self._med_buf) / len(self._med_buf) if self._med_buf else 0.0

        return SmoothedFrame(
            timestamp=frame.timestamp,
            poor_signal=frame.poor_signal,
            attention=round(smoothed_attn, 1),
            meditation=round(smoothed_med, 1),
            raw_wave=frame.raw_wave,
            alpha_power=round(smoothed_alpha, 1),
            eeg_bands=eeg_bands,
        )

    def _compute_alpha_from_raw(self, samples: list[int]) -> float:
        """Compute alpha band (8-12 Hz) power from raw_wave samples @ 512 Hz.

        Uses a simple biquad bandpass filter (10 Hz center, Q=2.0) followed
        by RMS power of the filtered signal.
        """
        if not samples:
            return 0.0

        # Lazy-init biquad state
        if not hasattr(self, '_bq_z1'):
            # Biquad bandpass coeffs: 10 Hz, fs=512, Q=2.0
            import math as _m
            f0, fs, Q = 10.0, 512.0, 2.0
            w0 = 2 * _m.pi * f0 / fs
            alpha = _m.sin(w0) / (2 * Q)
            a0 = 1 + alpha
            self._bq_b0 = alpha / a0
            self._bq_b1 = 0.0
            self._bq_b2 = -alpha / a0
            self._bq_a1 = (-2 * _m.cos(w0)) / a0
            self._bq_a2 = (1 - alpha) / a0
            self._bq_z1 = 0.0
            self._bq_z2 = 0.0

        # Run filter over all samples, accumulate squared output
        # Skip saturated ADC values (TGAM hardware glitch)
        power_sum = 0.0
        count = 0
        skipped = 0
        for x in samples:
            if abs(x) >= 2000:
                skipped += 1
                continue
            y = (self._bq_b0 * x + self._bq_b1 * self._bq_z1 +
                 self._bq_b2 * self._bq_z2 -
                 self._bq_a1 * self._bq_z1 - self._bq_a2 * self._bq_z2)
            self._bq_z2 = self._bq_z1
            self._bq_z1 = x
            power_sum += y * y
            count += 1

        if count == 0:
            return 0.0
        import math as _m
        return _m.sqrt(power_sum / count) * 0.08
