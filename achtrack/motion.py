"""Motion that can change its mind: springs in place of fixed-length animations."""
from __future__ import annotations

import ctypes
import math
import os
import subprocess
import sys
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal

FRAME_MS = 16


class Spring(QObject):
    """A critically damped spring: it settles without overshoot and has no set length.

    A new target carries on from the current position and speed, so anything moving on one can
    be sent somewhere else at any moment without a jump. `response` is roughly how many seconds
    it takes to get there; `precision` is how close counts as arrived.
    """
    changed = Signal(float)
    settled = Signal()

    def __init__(self, value: float = 0.0, response: float = 0.4, precision: float = 0.5, parent=None):
        super().__init__(parent)
        self.value = float(value)
        self.velocity = 0.0
        self.target = float(value)
        self.response = response
        self.precision = precision
        self._last = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    def go(self, target: float, response: float | None = None, velocity: float | None = None) -> None:
        self.target = float(target)
        if response is not None:
            self.response = response
        if velocity is not None:
            self.velocity = float(velocity)
        if not self._timer.isActive():
            self._last = time.monotonic()
            self._timer.start()

    def snap(self, value: float) -> None:
        """Puts it somewhere at once, for following a pointer; whatever it was doing stops."""
        self.stop()
        self.value = self.target = float(value)
        self.velocity = 0.0
        self.changed.emit(self.value)

    def stop(self) -> None:
        self._timer.stop()

    def step(self, dt: float) -> None:
        omega = 2 * math.pi / max(self.response, 0.001)
        away = self.value - self.target
        rate = self.velocity + omega * away
        decay = math.exp(-omega * dt)
        self.value = self.target + (away + rate * dt) * decay
        self.velocity = (rate - omega * (away + rate * dt)) * decay
        arrived = abs(self.value - self.target) < self.precision and \
            abs(self.velocity) < self.precision * omega
        if arrived:
            self.value = self.target
            self.velocity = 0.0
            self.stop()
        self.changed.emit(self.value)
        if arrived:
            self.settled.emit()

    def _tick(self) -> None:
        now = time.monotonic()
        self.step(min(now - self._last, 0.05))      # after a stall, carry on rather than leap
        self._last = now


def project(velocity: float, deceleration: float = 0.998) -> float:
    """How much further a flick would carry on by itself, in the same units as a scroll."""
    return velocity / 1000 * deceleration / (1 - deceleration)


def rubberband(overshoot: float, dimension: float, constant: float = 0.55) -> float:
    """Past a limit, the further you pull the less it follows - it never quite stops dead."""
    return overshoot * dimension * constant / (dimension + constant * abs(overshoot))


_reduced: tuple = (0.0, False)


def reduced() -> bool:
    """Whether the system asks for less motion. Asked at most once a minute."""
    global _reduced
    now = time.monotonic()
    if not _reduced[0] or now - _reduced[0] > 60:
        _reduced = (now, _ask_reduced())
    return _reduced[1]


def _ask_reduced() -> bool:
    try:
        if os.name == "nt":
            enabled = ctypes.c_int(1)
            ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)   # client-area animation
            return not enabled.value
        command = (["defaults", "read", "com.apple.universalaccess", "reduceMotion"] if sys.platform == "darwin"
                   else ["gsettings", "get", "org.gnome.desktop.interface", "enable-animations"])
        out = subprocess.run(command, capture_output=True, timeout=2).stdout.decode().strip()
        return out == ("1" if sys.platform == "darwin" else "false")
    except (OSError, AttributeError, subprocess.SubprocessError):
        return False
