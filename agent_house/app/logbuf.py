"""In-memory log ring buffer shown in GUI → Settings & Logs and shipped in the diagnostics bundle."""

from __future__ import annotations

import logging
from collections import deque

LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}


class RingHandler(logging.Handler):
    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self.records: deque[dict] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            self.records.append(
                {
                    "ts": record.created,
                    "level": record.levelname.lower(),
                    "logger": record.name,
                    "message": message,
                }
            )
        except Exception:  # noqa: BLE001 - logging must never raise
            pass

    def tail(self, limit: int = 200, level: str = "debug") -> list[dict]:
        floor = LEVELS.get(level, 10)
        rows = [r for r in self.records if LEVELS.get(r["level"], 20) >= floor]
        return rows[-limit:]


ring = RingHandler()


def setup_logging(level: str = "info") -> None:
    root = logging.getLogger()
    root.setLevel(LEVELS.get(level.lower(), 20))
    if ring not in root.handlers:
        root.addHandler(ring)
    if not any(isinstance(h, logging.StreamHandler) and h is not ring for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(stream)
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
