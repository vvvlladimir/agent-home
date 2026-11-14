"""House time: what "now" means for the add-on.

In production this is the system clock, nothing more. The polygon (`sim/days.py`) moves it forward to
live weeks of a household in minutes, in jumps between events, like a discrete-event simulation; the
fake HA reads the same clock, so both sides agree on the date.

Only wall-clock meaning goes through here: timestamps, "today", daily slots, undo windows, history
ranges. Durations of real work (timeouts, latency, rate windows, back-off) keep `time.monotonic()`
and `asyncio.sleep()`, so a slow model call is never "hours long" in house time.

The offset only ever grows: the house never goes back in time, so ordering in the store holds.
"""

from __future__ import annotations

import threading
import time as _time
from datetime import UTC, datetime, tzinfo

_lock = threading.Lock()
_offset = 0.0  # seconds added to the system clock; 0 in production


def time() -> float:
    """Seconds since the epoch, house time (drop-in for `time.time()`)."""
    return _time.time() + _offset


def monotonic() -> float:
    """`time.monotonic()` plus the same offset: cooldowns and hourly caps that count house time."""
    return _time.monotonic() + _offset


def now(tz: tzinfo | None = None) -> datetime:
    """House time as a datetime: aware in `tz`, or naive local like `datetime.now()`."""
    return datetime.fromtimestamp(time(), tz)


def local() -> datetime:
    """Aware local house time (drop-in for `datetime.now().astimezone()`)."""
    return datetime.fromtimestamp(time(), UTC).astimezone()


def offset() -> float:
    return _offset


def advance(seconds: float) -> float:
    """Move house time forward (simulation only); returns the new house time."""
    global _offset
    if seconds < 0:
        raise ValueError("house time never goes back")
    with _lock:
        _offset += seconds
        return _time.time() + _offset


def travel_to(ts: float) -> float:
    """Jump house time to `ts` if that is ahead of it; a target in the past is a no-op."""
    with _lock:
        ahead = ts - (_time.time() + _offset)
    return advance(ahead) if ahead > 0 else time()


def reset() -> None:
    """Back to the system clock (tests only)."""
    global _offset
    with _lock:
        _offset = 0.0
