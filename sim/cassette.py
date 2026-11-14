"""Record and replay the models' answers at the sim gateways (step 4: verification by restore).

An ASGI wrapper around a gateway app. Every POST is keyed by its path and body with the volatile
parts blanked (clock times, dates, epoch stamps, run ids), so the same question asked a minute later
still matches. Answers < 400 are recorded; after `load()`, a matching request is answered from the
tape first (in order, for questions asked more than once) and only then forwarded. A snapshot
(sim/snapshot.py) saves the tape, so a restored polygon's models answer the way they did.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from pathlib import Path

VOLATILE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"  # ISO stamps
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"  # clock times
    r"|\b\d{4}-\d{2}-\d{2}\b"  # dates
    r"|\b1[6-9]\d{8}(?:\.\d+)?\b"  # epoch seconds
    r"|\b[rpa]_[0-9a-f]{12,}\b"  # run / proposal / action ids
)


def key(path: str, body: bytes) -> str:
    text = VOLATILE.sub("~", body.decode("utf-8", "replace"))
    return hashlib.sha256(f"{path}\n{text}".encode()).hexdigest()[:32]


class Cassette:
    def __init__(self) -> None:
        self.tape: dict[str, list[dict]] = {}  # everything answered (< 400) this run
        self.queue: dict[str, deque[dict]] = {}  # loaded answers still to replay
        self.hits = self.misses = 0

    def take(self, k: str) -> dict | None:
        q = self.queue.get(k)
        if q:
            self.hits += 1
            return q.popleft()
        self.misses += 1
        return None

    def record(self, k: str, status: int, headers: list, body: bytes) -> None:
        if status < 400:
            self.tape.setdefault(k, []).append(
                {
                    "status": status,
                    "headers": [[a.decode(), b.decode()] for a, b in headers],
                    "body": body.decode("utf-8", "replace"),
                }
            )

    def save(self, path: Path) -> int:
        path.write_text(json.dumps(self.tape))
        return sum(len(v) for v in self.tape.values())

    def load(self, path: Path) -> int:
        tape = json.loads(path.read_text()) if path.exists() else {}
        self.queue = {k: deque(v) for k, v in tape.items()}
        return sum(len(v) for v in tape.values())

    def wrap(self, app):
        """The gateway app with record/replay in front of it (POST only; GETs pass through)."""

        async def asgi(scope, receive, send):
            if scope["type"] != "http" or scope["method"] != "POST":
                return await app(scope, receive, send)
            body, more = b"", True
            while more:
                msg = await receive()
                body += msg.get("body", b"")
                more = msg.get("more_body", False)
            k = key(scope["path"], body)
            hit = self.take(k)
            if hit is not None:
                headers = [
                    (a.encode(), b.encode())
                    for a, b in hit["headers"]
                    if a.lower() not in ("content-length", "transfer-encoding")
                ]
                data = hit["body"].encode()
                headers.append((b"content-length", str(len(data)).encode()))
                await send({"type": "http.response.start", "status": hit["status"], "headers": headers})
                await send({"type": "http.response.body", "body": data})
                return
            given = False

            async def replay_receive():
                nonlocal given
                if not given:
                    given = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            status, headers, chunks = 500, [], []

            async def capture(msg):
                nonlocal status, headers
                if msg["type"] == "http.response.start":
                    status, headers = msg["status"], list(msg.get("headers") or [])
                elif msg["type"] == "http.response.body":
                    chunks.append(msg.get("body", b""))
                await send(msg)

            await app(scope, replay_receive, capture)
            self.record(k, status, headers, b"".join(chunks))

        return asgi
