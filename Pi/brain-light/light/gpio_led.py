#!/usr/bin/env python3
"""GPIO LED driver — PWM brightness control on BCM GPIO 17.

Hardware
--------
    LED anode (long leg)  → GPIO 17 (Pin 11)
    LED cathode (short leg) → 220 Ω resistor → GND (Pin 9)

Provides a simple, dependency-light verification path before the
WiZ bulb is integrated.  Also useful as a secondary local indicator
during development.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# PWM base frequency in Hz
_PWM_FREQ = 100


class GpioLed:
    """Single-colour LED driven by hardware PWM on GPIO 17.

    Parameters
    ----------
    pin : int
        BCM pin number (default 17).
    pwm_freq : int
        PWM frequency in Hz (default 100).
    simulate : bool
        When True, only log actions — safe for non-Pi machines.
    """

    def __init__(self, pin: int = 17, pwm_freq: int = _PWM_FREQ, simulate: bool = False) -> None:
        self._pin = pin
        self._simulate = simulate
        self._pwm = None
        self._brightness: float = 0.0   # 0.0–100.0

        if simulate:
            logger.info("GPIO LED running in SIMULATE mode")
            return

        try:
            import RPi.GPIO as GPIO
            self._GPIO = GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            # Release any stale state from previous runs
            try:
                GPIO.cleanup(pin)
            except Exception:
                pass
            GPIO.setup(pin, GPIO.OUT)
            self._pwm = GPIO.PWM(pin, pwm_freq)
            self._pwm.start(0)
            # Quick boot blink so user knows LED is alive
            self.set(100)
            time.sleep(0.15)
            self.set(10)
            time.sleep(0.15)
            self.set(50)
            logger.info("GPIO LED initialised on BCM %d @ %d Hz", pin, pwm_freq)
        except ImportError:
            logger.warning("RPi.GPIO not available — LED in SIMULATE mode")
            self._simulate = True
        except RuntimeError as exc:
            logger.warning("GPIO init failed (%s) — LED in SIMULATE mode", exc)
            self._simulate = True

    # ── public API ──────────────────────────────────────────────────────

    @property
    def brightness(self) -> float:
        """Current brightness 0–100 %."""
        return self._brightness

    def set(self, brightness: float) -> None:
        """Set LED brightness.

        Parameters
        ----------
        brightness : float
            0–100, where 0 = off and 100 = maximum.
        """
        brightness = max(0.0, min(100.0, float(brightness)))
        self._brightness = brightness
        duty = brightness
        logger.info("LED set → %.0f %%", brightness)

        if self._simulate:
            return

        try:
            if self._pwm is not None:
                self._pwm.ChangeDutyCycle(duty)
            else:
                logger.warning("LED _pwm is None — cannot set!")
        except Exception as exc:
            logger.error("LED PWM error: %s", exc)

    def on(self) -> None:
        """Full brightness."""
        self.set(100.0)

    def off(self) -> None:
        """Turn LED off."""
        self.set(0.0)

    def pulse(self, times: int = 3, interval: float = 0.3) -> None:
        """Quick pulse for visual confirmation (e.g. boot indicator)."""
        for i in range(times):
            self.on()
            time.sleep(interval / 2)
            self.off()
            time.sleep(interval / 2)

    def fade_to(self, target: float, duration: float = 1.0, steps: int = 30) -> None:
        """Smoothly transition brightness to *target* over *duration* seconds."""
        start = self._brightness
        if abs(target - start) < 0.5 or duration <= 0:
            self.set(target)
            return

        for i in range(1, steps + 1):
            frac = i / steps
            # ease-in-out cubic
            eased = frac ** 3 / (frac ** 3 + (1 - frac) ** 3) if 0 < frac < 1 else frac
            val = start + (target - start) * eased
            self.set(val)
            time.sleep(duration / steps)

    def cleanup(self) -> None:
        """Release GPIO resources."""
        self.off()
        if not self._simulate and self._pwm is not None:
            try:
                self._pwm.stop()
            except Exception:
                pass
        if not self._simulate:
            try:
                self._GPIO.cleanup(self._pin)
            except Exception:
                pass
        logger.info("GPIO LED cleaned up")
