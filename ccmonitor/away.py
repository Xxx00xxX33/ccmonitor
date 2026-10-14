"""Away state (GLOSSARY: 离开状态): 3 minutes without keyboard or mouse input, or
declared from the side channel, either with /away or implicitly by replying or tapping
a button on the phone. A declaration lasts until the next input (DESIGN.md 3.7). A phone
reply no longer needs a quiet computer (user's call, 2026-10-14): it was a minute's wait
for nothing when the phone is in hand.

The write-back driver itself nudges the mouse to wake a display that went off (DESIGN.md
3.5); that input is ccmonitor's own and must not count as someone at the computer. The
driver reports the nudge only when it exits, so the whole run is treated as ccmonitor's
own: otherwise a check during the run reads the nudge as the user (seen live 2026-10-12,
a /away declaration cancelled seconds later).
"""
import ctypes
import sys
import threading
from typing import Callable

IDLE_SECONDS = 180  # user's call, 2026-10-12: 10 minutes, then 1 (too many switches), then 3
OWN_INPUTS = 50  # own inputs remembered


def _tick() -> int:
    return ctypes.windll.kernel32.GetTickCount() if sys.platform == "win32" else 0


def _last_input() -> int:
    if sys.platform != "win32":
        return 0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return info.dwTime


class Presence:
    def __init__(self, idle_seconds: int = IDLE_SECONDS,
                 now: Callable[[], int] = _tick, last_input: Callable[[], int] = _last_input):
        self._idle_ms = idle_seconds * 1000
        self._now = now
        self._last_input = last_input
        self._declared_at: int | None = None
        self._own: dict[int, int] = {}  # tick of an input ccmonitor made -> last input before it
        self._lock = threading.RLock()  # the driver runs on the side channel's worker thread
        self._runs = 0                  # driver runs in progress
        self._run_started: int | None = None
        self._run_before: int | None = None  # last real input before the run began

    def set_idle_seconds(self, seconds: int) -> None:
        """The away threshold (离开时间), changed while running (#69)."""
        self._idle_ms = seconds * 1000

    def begin_own_run(self) -> None:
        """ccmonitor is about to drive the screen. Until the run ends, input is treated as
        its own: the driver nudges the mouse at the start but only says so when it exits."""
        with self._lock:
            if self._runs == 0:
                self._run_before = self._real_last_input()
                self._run_started = self._now()
            self._runs += 1

    def end_own_run(self) -> None:
        with self._lock:
            self._runs = max(0, self._runs - 1)
            if self._runs == 0 and self._run_before is not None:
                # Fold the run into the chain so later checks still skip its input.
                self.ignore_input(self._run_before, self._last_input())
                self._run_started = self._run_before = None

    def _during_own_run(self, last: int) -> bool:
        return (self._runs > 0 and self._run_started is not None
                and (last - self._run_started) & 0xFFFFFFFF <= 0x7FFFFFFF)

    def ignore_input(self, before: int, own: int) -> None:
        """ccmonitor made an input at tick `own`; the last real input was at `before`."""
        if own != before:
            self._own[own] = before
            while len(self._own) > OWN_INPUTS:
                del self._own[next(iter(self._own))]

    def _real_last_input(self) -> int:
        last = self._last_input()
        with self._lock:
            if self._during_own_run(last):
                return self._run_before if self._run_before is not None else last
        seen = set()
        while last in self._own and last not in seen:  # nudges in a row chain back
            seen.add(last)
            last = self._own[last]
        return last

    def declare_away(self) -> None:
        self._declared_at = self._now()

    def quiet_seconds(self) -> float:
        """Seconds since the last keyboard or mouse input."""
        return ((self._now() - self._real_last_input()) & 0xFFFFFFFF) / 1000

    def declare_back(self) -> None:
        self._declared_at = None

    def away_source(self) -> str | None:
        """Why the user counts as away: "idle" (no input for the away threshold), "declared"
        (/away, or acting from the phone), or None when at the computer."""
        if not self.is_away():
            return None
        if (self._now() - self._real_last_input()) & 0xFFFFFFFF >= self._idle_ms:
            return "idle"
        return "declared"

    def is_away(self) -> bool:
        last = self._real_last_input()
        # Tick counters are 32-bit and wrap every ~49.7 days; mask the difference.
        if (self._now() - last) & 0xFFFFFFFF >= self._idle_ms:
            return True
        if self._declared_at is not None:
            since = (last - self._declared_at) & 0xFFFFFFFF
            if since == 0 or since > 0x7FFFFFFF:
                return True  # last input is not after the declaration
        self._declared_at = None
        return False
