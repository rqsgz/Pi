#!/usr/bin/env python3
"""ThinkGear Packet Parser — byte-stream state machine.

Serialises the TGAM Bluetooth SPP stream (57600 bps 8N1) into
structured dicts.  Every packet is framed by the sync-word pair
0xAA 0xAA followed by a payload-length byte, N payload bytes,
and a 1-byte checksum.

Checksum:   CHK = ~(sum(payload_bytes) & 0xFF)
            → (CHK + sum(payload_bytes)) & 0xFF == 0   is valid.

Payload codes (multi-value packets interleave CODE|VLEN|DATA…):
    0x02   signal-quality    1 B   0=perfect  200=no-contact  >50 discard
    0x04   attention         1 B   eSense 0–100
    0x05   meditation        1 B   eSense 0–100
    0x80   raw-wave          2 B   big-endian signed 16-bit  512 Hz
    0x83   eeg-power        24 B   8 bands × 3 B big-endian unsigned

Author: NeuroLux / buckfpga.uk
"""

from __future__ import annotations

import enum
import logging
import struct
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────
SYNC_BYTE = 0xAA
MAX_PAYLOAD = 172  # ThinkGear spec: payload never exceeds 172 bytes

# Code → (name, length-in-bytes, struct-fmt)
_CODE_MAP: dict[int, tuple[str, int, Optional[str]]] = {
    0x02: ("poor_signal", 1, "!B"),
    0x04: ("attention", 1, "!B"),
    0x05: ("meditation", 1, "!B"),
    0x80: ("raw_wave", 2, "!h"),
    0x83: (
        "eeg_power",
        24,
        None,  # special – 8×3B big-endian unsigned
    ),
}

# EEG band order inside 0x83 payload
EEG_BANDS = [
    "delta",
    "theta",
    "low_alpha",
    "high_alpha",
    "low_beta",
    "high_beta",
    "low_gamma",
    "mid_gamma",
]

# ── state machine ──────────────────────────────────────────────────────
class State(enum.IntEnum):
    WAIT_SYNC1 = 0
    WAIT_SYNC2 = 1
    WAIT_PLEN = 2
    READ_PAYLOAD = 3
    VERIFY = 4


@dataclass
class BrainFrame:
    """One fully-parsed frame from TGAM.

    All fields are optional — the TGAM sends different codes at
    different rates (e.g. raw_wave @512 Hz, eeg_power @1 Hz).
    """

    timestamp: float = 0.0
    poor_signal: int = 0          # 0–200
    attention: Optional[int] = None       # 0–100
    meditation: Optional[int] = None      # 0–100
    raw_wave: Optional[int] = None        # signed 16-bit
    eeg_power: dict[str, int] = field(default_factory=dict)  # band → value
    checksum_ok: bool = True
    raw_payload: bytes = b""


class ThinkGearParser:
    """Byte-stream parser for ThinkGear protocol.

    Usage
    -----
        parser = ThinkGearParser()
        for byte_chunk in serial_stream:
            for frame in parser.feed(byte_chunk):
                print(frame.attention, frame.meditation)
    """

    def __init__(self) -> None:
        self._state = State.WAIT_SYNC1
        self._payload_len = 0
        self._payload_buf = bytearray()
        self._total_packets = 0
        self._bad_checksum = 0
        self._last_values: dict[str, int | float] = {}  # carry-forward
        self._last_update_time: dict[str, float] = {}    # when each key was updated
        self._carry_forward_ttl: float = 5.0             # max seconds to carry forward

    # ── public ──────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, int]:
        return {
            "total_packets": self._total_packets,
            "bad_checksum": self._bad_checksum,
            "checksum_pass_rate": (
                1.0
                if self._total_packets == 0
                else (self._total_packets - self._bad_checksum) / self._total_packets
            ),
        }

    def reset(self) -> None:
        """Reset parser state — call on TGAM disconnect/reconnect to
        prevent stale carry-forward values from leaking across sessions.
        """
        self._state = State.WAIT_SYNC1
        self._payload_len = 0
        self._payload_buf = bytearray()
        self._last_values.clear()
        self._last_update_time.clear()
        logger.info("ThinkGearParser reset — state & carry-forward cleared")

    def feed(self, data: bytes) -> list[BrainFrame]:
        """Push raw bytes in; receive completed frames out."""
        frames: list[BrainFrame] = []
        for b in data:
            frame = self._consume(b)
            if frame is not None:
                frames.append(frame)
        return frames

    # ── internals ───────────────────────────────────────────────────

    def _consume(self, byte: int) -> Optional[BrainFrame]:
        """Per-byte state-machine tick.  Returns a frame when one completes."""
        if self._state == State.WAIT_SYNC1:
            if byte == SYNC_BYTE:
                self._state = State.WAIT_SYNC2
            return None

        if self._state == State.WAIT_SYNC2:
            if byte == SYNC_BYTE:
                self._state = State.WAIT_PLEN
            else:
                self._state = State.WAIT_SYNC1  # false sync – restart
            return None

        if self._state == State.WAIT_PLEN:
            if byte == SYNC_BYTE:
                # Stale SYNC — stay here, re-use as next SYNC1
                return None
            if byte > MAX_PAYLOAD:
                logger.debug("PLEN %d > %d — resetting", byte, MAX_PAYLOAD)
                self._state = State.WAIT_SYNC1
                return None
            self._payload_len = byte
            self._payload_buf = bytearray()
            self._state = State.READ_PAYLOAD
            return None

        if self._state == State.READ_PAYLOAD:
            self._payload_buf.append(byte)
            if len(self._payload_buf) >= self._payload_len:
                self._state = State.VERIFY
            return None

        if self._state == State.VERIFY:
            self._state = State.WAIT_SYNC1
            chk = byte
            expected = (~sum(self._payload_buf) & 0xFF)
            ok = (chk == expected)
            if not ok:
                self._bad_checksum += 1
                logger.debug(
                    "Checksum fail: got 0x%02X expected 0x%02X", chk, expected
                )
            self._total_packets += 1
            return self._decode(bytes(self._payload_buf), ok)

        return None  # unreachable

    def _decode(self, payload: bytes, checksum_ok: bool) -> BrainFrame:
        """Walk the payload extracting known code-value pairs."""
        import time

        frame = BrainFrame(timestamp=time.time(), checksum_ok=checksum_ok,
                           raw_payload=payload)

        pos = 0
        while pos < len(payload):
            code = payload[pos]
            if code not in _CODE_MAP:
                # unknown code — skip its vlen + data
                if pos + 1 < len(payload):
                    vlen = payload[pos + 1]
                    pos += 2 + vlen
                else:
                    break
                continue

            name, vlen, fmt = _CODE_MAP[code]
            if pos + 2 + vlen > len(payload):
                break

            data = payload[pos + 2 : pos + 2 + vlen]

            if code == 0x83:  # EEG power — 8 × 3-byte big-endian
                eeg: dict[str, int] = {}
                for i, band in enumerate(EEG_BANDS):
                    start = i * 3
                    if start + 3 <= len(data):
                        val = (data[start] << 16) | (data[start + 1] << 8) | data[start + 2]
                        eeg[band] = val
                frame.eeg_power = eeg
                self._last_values["eeg_power"] = eeg
                self._last_update_time["eeg_power"] = time.time()
            elif fmt is not None:
                val = struct.unpack(fmt, data)[0]
                setattr(frame, name, val)
                self._last_values[name] = val
                self._last_update_time[name] = time.time()

            pos += 2 + vlen  # code-byte + vlen-byte + data

        # carry-forward missing values (with TTL — don't replay stale data forever)
        now = time.time()
        for key, val in self._last_values.items():
            # Skip if this value hasn't been updated within TTL
            last_up = self._last_update_time.get(key, 0.0)
            if now - last_up > self._carry_forward_ttl:
                continue
            if key == "eeg_power":
                if not frame.eeg_power:
                    frame.eeg_power = val if isinstance(val, dict) else {}
            elif key not in ("poor_signal",):
                if getattr(frame, key) is None:
                    setattr(frame, key, val)

        return frame
