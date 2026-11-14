"""Record and replay of model answers (sim/cassette.py)."""

from __future__ import annotations

import httpx

from sim.cassette import Cassette, key


def test_the_key_ignores_times_dates_and_ids():
    a = b'{"messages": [{"content": "Time: 2026-11-14 13:05 (Sat). Run r_1a1da2c25e83c0613 at 1794664290.1"}]}'
    b = b'{"messages": [{"content": "Time: 2026-11-15 09:41 (Sat). Run r_1a1da2c25e83c0999 at 1794787200.0"}]}'
    assert key("/v1/chat/completions", a) == key("/v1/chat/completions", b)
    assert key("/v1/chat/completions", a) != key("/v1/messages", a)
    assert key("/x", b'{"q": "lamp on"}') != key("/x", b'{"q": "lamp off"}')


async def test_answers_are_recorded_and_replayed_in_order(tmp_path):
    calls = []

    async def upstream(scope, receive, send):
        calls.append(scope["path"])
        body = f'{{"n": {len(calls)}}}'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    first = Cassette()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first.wrap(upstream)), base_url="http://t"
    ) as c:
        assert (await c.post("/v1/chat", content=b"same")).json() == {"n": 1}
        assert (await c.post("/v1/chat", content=b"same")).json() == {"n": 2}
    first.save(tmp_path / "tape.json")

    second = Cassette()
    assert second.load(tmp_path / "tape.json") == 2
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second.wrap(upstream)), base_url="http://t"
    ) as c:
        assert [(await c.post("/v1/chat", content=b"same")).json()["n"] for _ in range(3)] == [1, 2, 3]
    assert second.hits == 2 and second.misses == 1 and len(calls) == 3, "the third went upstream"
