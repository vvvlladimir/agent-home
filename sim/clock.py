"""The add-on's house clock (agent_house/app/core/clock.py), shared in-process: the fake HA and the
add-on read the same "now", and `sim/days.py` moves it forward to live days in minutes."""

from __future__ import annotations

import sys
from pathlib import Path

_ADDON = str(Path(__file__).resolve().parents[1] / "agent_house")
if _ADDON not in sys.path:
    sys.path.insert(0, _ADDON)

from app.core.clock import advance, local, monotonic, now, offset, reset, time, travel_to  # noqa: E402

__all__ = ["advance", "local", "monotonic", "now", "offset", "reset", "time", "travel_to"]
