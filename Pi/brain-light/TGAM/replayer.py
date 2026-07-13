#!/usr/bin/env python3
"""CSV offline replayer — iterate a recorded session frame-by-frame.

Lets you develop and debug signal-processing / light-control code
**without** wearing the TGAM headset.

CSV format (one row per frame, header on line 1):
    ts,attention,meditation,poor_signal,raw_wave,delta,theta,
    low_alpha,high_alpha,low_beta,high_beta,low_gamma,mid_gamma

Usage
-----
    from tgam.replayer import Replayer
    rep = Replayer("data/session_20260711.csv")
    for frame in rep:
        print(f"{frame.attention=} {frame.meditation=}")

    # Or as an asyncio generator:
    async for frame in rep.async_iter():
        ...
"""

from __future__ import annotations

import asyncio
import csv
import logging
import time
from pathlib import Path
from typing import Iterator, Optional

from .parser import BrainFrame, EEG_BANDS

logger = logging.getLogger(__name__)

_CSV_COLS = [
    "ts",
    "attention",
    "meditation",
    "poor_signal",
    "raw_wave",
    "delta",
    "theta",
    "low_alpha",
    "high_alpha",
    "low_beta",
    "high_beta",
    "low_gamma",
    "mid_gamma",
]


class Replayer:
    """Iterable source of BrainFrames from a recorded CSV file.

    Parameters
    ----------
    path : str | Path
        Path to CSV session file.
    speed : float
        Playback speed multiplier.  1.0 = real-time, 2.0 = double speed,
        0 = as-fast-as-possible (ignore timestamps).
    loop : bool
        When True, restart from the beginning after EOF.
    """

    def __init__(
        self,
        path: str | Path,
        speed: float = 1.0,
        loop: bool = False,
    ) -> None:
        self.path = Path(path)
        self.speed = speed
        self.loop = loop
        self._rows: list[dict[str, str]] = []
        self._loaded = False
        self._start_wall: float = 0.0
        self._start_csv: float = 0.0

    # ── load ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._loaded:
            return
        if not self.path.exists():
            raise FileNotFoundError(f"CSV not found: {self.path}")

        with open(self.path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            self._rows = list(reader)

        if not self._rows:
            raise ValueError(f"CSV is empty: {self.path}")

        self._loaded = True
        logger.info("Replayer loaded %d frames from %s", len(self._rows), self.path)

    # ── iterate ──────────────────────────────────────────────────────

    def __iter__(self) -> Iterator[BrainFrame]:
        self._load()
        self._start_wall = time.time()
        self._start_csv = float(self._rows[0].get("ts", 0))
        return self

    def __next__(self) -> BrainFrame:
        self._load()
        return self._next_impl()

    def _next_impl(self) -> BrainFrame:
        while True:
            if not self._rows:
                raise StopIteration

            row = self._rows.pop(0)

            # Honour inter-frame gap when speed > 0
            if self.speed > 0:
                csv_ts = float(row.get("ts", 0))
                elapsed_csv = csv_ts - self._start_csv
                elapsed_wall = time.time() - self._start_wall
                sleep_s = (elapsed_csv / self.speed) - elapsed_wall
                if sleep_s > 0:
                    time.sleep(sleep_s)

            frame = self._row_to_frame(row)

            # loop → push a clone to the end
            if self.loop:
                self._rows.append(row)

            return frame

    # ── async iterator ───────────────────────────────────────────────

    async def async_iter(self):
        """Async generator – yields frames with asyncio.sleep gaps."""
        self._load()
        if not self._rows:
            return

        start_wall = time.time()
        start_csv = float(self._rows[0].get("ts", 0))
        rows = list(self._rows)

        for row in rows:
            if self.speed > 0:
                csv_ts = float(row.get("ts", 0))
                elapsed_csv = csv_ts - start_csv
                elapsed_wall = time.time() - start_wall
                sleep_s = (elapsed_csv / self.speed) - elapsed_wall
                if sleep_s > 0:
                    await asyncio.sleep(sleep_s)
            yield self._row_to_frame(row)

    # ── helpers ──────────────────────────────────────────────────────

    def _row_to_frame(self, row: dict[str, str]) -> BrainFrame:
        def _f(key: str, default: int = 0) -> int:
            try:
                return int(float(row.get(key, default)))
            except (ValueError, TypeError):
                return default

        eeg = {
            "delta": _f("delta"),
            "theta": _f("theta"),
            "low_alpha": _f("low_alpha"),
            "high_alpha": _f("high_alpha"),
            "low_beta": _f("low_beta"),
            "high_beta": _f("high_beta"),
            "low_gamma": _f("low_gamma"),
            "mid_gamma": _f("mid_gamma"),
        }

        return BrainFrame(
            timestamp=float(row.get("ts", time.time())),
            poor_signal=_f("poor_signal"),
            attention=_f("attention"),
            meditation=_f("meditation"),
            raw_wave=_f("raw_wave"),
            eeg_power=eeg,
            checksum_ok=True,
        )


# ── CSV writer helper ───────────────────────────────────────────────────
def write_frame_row(writer, frame: BrainFrame) -> None:
    """Append a BrainFrame as one row in a csv.DictWriter."""
    row: dict[str, str | int | float] = {
        "ts": f"{frame.timestamp:.3f}",
        "attention": frame.attention if frame.attention is not None else "",
        "meditation": frame.meditation if frame.meditation is not None else "",
        "poor_signal": frame.poor_signal,
        "raw_wave": frame.raw_wave if frame.raw_wave is not None else "",
    }
    for band in EEG_BANDS:
        row[band] = frame.eeg_power.get(band, "")
    writer.writerow(row)
