"""LLM manager against the fake llama-server: queue order, coalescing, stale drop, retries, health."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import jsonschema
import pytest
import yaml

from app.config import RuntimeSettings, Settings
from app.core.llm import LLMDropped, LLMError, LLMManager, LLMUnavailable, Priority

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())
TOOLS = [
    {"type": "function", "function": {"name": "call_service", "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "report", "parameters": {"type": "object"}}},
]


def say(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def asked(sim) -> list[str]:
    return [r["messages"][-1]["content"] for r in sim.requests]


@pytest.fixture
async def make_llm(tmp_path, llm_server):
    made: list[LLMManager] = []

    async def make(url: str | None = None, **runtime) -> LLMManager:
        settings = Settings(data_dir=tmp_path, jetson_llm_url=llm_server.url if url is None else url)
        llm = LLMManager(settings, RuntimeSettings(**runtime), health_interval=0.05, retry_backoff=0.01)
        await llm.start()
        await llm.probe()
        made.append(llm)
        return llm

    yield make
    for llm in made:
        await llm.stop()


async def busy(llm: LLMManager, sim, seconds: float = 0.4) -> asyncio.Task:
    """Occupy the single slot with a slow request and wait until it is in flight."""
    sim.script.append({"text": "slow", "delay": seconds})
    task = asyncio.create_task(llm.chat(say("slow"), priority=Priority.INTERACTIVE))
    for _ in range(100):
        if llm.status()["in_flight"]:
            break
        await asyncio.sleep(0.01)
    return task


async def test_unconfigured_fails_fast(make_llm):
    llm = await make_llm(url="")
    assert llm.status()["state"] == "unconfigured"
    with pytest.raises(LLMUnavailable):
        await llm.chat(say("hi"))


async def test_chat_tool_calls_metrics_and_status_contract(make_llm, llm_sim):
    llm = await make_llm()
    assert llm.state == "ok" and llm.model == llm_sim.model
    result = await llm.chat(say("binary_sensor.hall_motion is on, light.hall is off"), TOOLS)
    assert [c.name for c in result.tool_calls] == ["call_service"]
    assert result.tool_calls[0].arguments["entity_id"] == "light.hall"
    assert result.attempts == 1 and result.timings["decode_tps"] == llm_sim.tokens_per_second
    sent = llm_sim.requests[-1]
    assert sent["stream"] is True and sent["presence_penalty"] == 1.5 and sent["tools"] == TOOLS

    status = llm.status()
    schema = {**OPENAPI["components"]["schemas"]["LlmStatus"], "components": OPENAPI["components"]}
    jsonschema.validate(status, schema)
    assert status["requests_total"] == 1 and status["ttft_ms"] is not None


async def test_presence_penalty_override(make_llm, llm_sim):
    llm = await make_llm(llm_presence_penalty=0)
    await llm.chat(say("hi"))
    assert llm_sim.requests[-1]["presence_penalty"] == 0


async def test_priority_order(make_llm, llm_sim):
    llm = await make_llm()
    first = await busy(llm, llm_sim)
    jobs = [
        asyncio.create_task(llm.chat(say(name), priority=priority))
        for name, priority in (
            ("background", Priority.BACKGROUND),
            ("agent", Priority.AGENT),
            ("interactive", Priority.INTERACTIVE),
            ("agent2", Priority.AGENT),
        )
    ]
    await asyncio.gather(first, *jobs)
    assert asked(llm_sim) == ["slow", "interactive", "agent", "agent2", "background"]


async def test_coalescing_keeps_the_newest_in_the_oldest_place(make_llm, llm_sim):
    llm = await make_llm()
    first = await busy(llm, llm_sim)
    old = asyncio.create_task(llm.chat(say("kitchen v1"), coalesce_key="kitchen"))
    other = asyncio.create_task(llm.chat(say("bedroom"), coalesce_key="bedroom"))
    await asyncio.sleep(0.02)
    new = asyncio.create_task(llm.chat(say("kitchen v2"), coalesce_key="kitchen"))
    with pytest.raises(LLMDropped) as dropped:
        await old
    assert dropped.value.reason == "coalesced"
    await asyncio.gather(first, other, new)
    assert asked(llm_sim) == ["slow", "kitchen v2", "bedroom"]
    assert llm.status()["dropped_total"] == 1


async def test_stale_requests_are_dropped_not_answered_late(make_llm, llm_sim):
    llm = await make_llm(llm_stale_after_s=1)
    first = await busy(llm, llm_sim, 0.3)
    stale = asyncio.create_task(llm.chat(say("old news"), max_age_s=0.05))
    with pytest.raises(LLMDropped) as dropped:
        await stale
    assert dropped.value.reason == "stale"
    await first
    assert "old news" not in asked(llm_sim)


async def test_full_queue_drops_the_least_important(make_llm, llm_sim):
    llm = await make_llm(llm_max_queue=1)
    first = await busy(llm, llm_sim)
    background = asyncio.create_task(llm.chat(say("background"), priority=Priority.BACKGROUND))
    await asyncio.sleep(0.02)
    agent = asyncio.create_task(llm.chat(say("agent")))
    with pytest.raises(LLMDropped) as dropped:
        await background
    assert dropped.value.reason == "queue_full"
    with pytest.raises(LLMDropped):  # an equal or lower priority cannot push the agent out
        await llm.chat(say("another"), priority=Priority.BACKGROUND)
    await asyncio.gather(first, agent)


@pytest.mark.parametrize("failure", [{"status": 500}, {"garbage": True}])
async def test_server_errors_are_retried(make_llm, llm_sim, failure):
    llm = await make_llm(llm_max_retries=2)
    llm_sim.script.extend([failure, {"text": "fine"}])
    result = await llm.chat(say("hi"))
    assert result.text == "fine" and result.attempts == 2
    assert llm.status()["retries_total"] == 1


async def test_retries_give_up_and_client_errors_are_never_retried(make_llm, llm_sim):
    llm = await make_llm(llm_max_retries=1)
    llm_sim.script.extend([{"status": 500}] * 2)
    with pytest.raises(LLMError, match="after 2 attempts"):
        await llm.chat(say("hi"))
    llm_sim.script.append({"status": 400})
    with pytest.raises(LLMError, match="refused the request \\(HTTP 400\\): simulated failure"):
        await llm.chat(say("hi"))
    assert len(llm_sim.requests) == 3
    assert llm.status()["errors_total"] == 2


async def test_degraded_after_repeated_failures_and_back(make_llm, llm_sim):
    llm = await make_llm(llm_max_retries=2)
    llm_sim.script.extend([{"status": 500}] * 3)
    with pytest.raises(LLMError):
        await llm.chat(say("hi"))
    assert llm.state == "degraded"
    await llm.chat(say("hi"))
    assert llm.state == "ok"


async def test_timeout_is_an_error_without_retry(make_llm, llm_sim):
    llm = await make_llm()
    llm_sim.script.append({"text": "late", "hang": 2})
    with pytest.raises(LLMError, match="did not answer"):
        await llm.chat(say("hi"), timeout_s=0.3)
    assert len(llm_sim.requests) == 1


async def test_a_429_is_retried_and_reads_plainly_when_it_gives_up(make_llm, llm_sim):
    """QA-002: rate limits are retried with backoff, the error is readable, the state degrades."""
    llm = await make_llm(llm_max_retries=2)
    llm_sim.script.extend([{"status": 429}, {"text": "fine"}])
    assert (await llm.chat(say("hi"))).text == "fine" and llm.status()["retries_total"] == 1
    llm_sim.script.extend([{"status": 429}] * 3)
    with pytest.raises(LLMError, match="HTTP 429: too many requests") as err:
        await llm.chat(say("hi"))
    assert "{" not in str(err.value), "no raw JSON body"
    assert llm.state == "degraded" and llm.status()["last_error"]


async def test_the_wait_behind_a_stalled_request_counts_toward_the_timeout(make_llm, llm_sim):
    """QA-001: a chat queued behind a stall gets a plain timeout instead of waiting it out."""
    llm = await make_llm()
    llm_sim.script.append({"text": "stalled", "hang": 3})
    stalled = asyncio.create_task(llm.chat(say("stalled"), priority=Priority.AGENT, timeout_s=10))
    for _ in range(100):
        if llm.status()["in_flight"]:
            break
        await asyncio.sleep(0.01)
    assert llm.status()["in_flight_s"] is not None
    started = asyncio.get_running_loop().time()
    with pytest.raises(LLMError, match="did not answer within 0.5 s .*waited behind another request"):
        await llm.chat(say("owner"), priority=Priority.INTERACTIVE, timeout_s=0.5)
    assert asyncio.get_running_loop().time() - started < 2.5
    assert llm.status()["queue_depth"] == 0
    stalled.cancel()


async def test_requests_wait_while_the_model_loads(make_llm, llm_sim):
    llm = await make_llm()
    llm_sim.loading = True
    await llm.probe()
    assert llm.state == "warming"
    threading.Timer(0.3, lambda: setattr(llm_sim, "loading", False)).start()
    result = await llm.chat(say("hi"))
    assert result.text and llm.state == "ok"


async def test_still_loading_at_the_deadline_is_unavailable(make_llm, llm_sim):
    llm = await make_llm()
    llm_sim.loading = True
    with pytest.raises(LLMUnavailable, match="loading"):
        await llm.chat(say("hi"), timeout_s=0.3)
    llm_sim.loading = False


async def test_swap_reports_warming_until_healthy(make_llm, llm_sim):
    llm = await make_llm()
    llm.begin_swap("qwen35-2b-fast")
    assert llm.state == "warming" and llm.active_profile == "qwen35-2b-fast" and llm.swapping
    await llm.probe()
    assert llm.state == "ok" and not llm.swapping


async def test_unreachable_server_fails_fast(make_llm):
    llm = await make_llm(url="http://127.0.0.1:9")
    assert llm.state == "unreachable"
    with pytest.raises(LLMUnavailable):
        await asyncio.wait_for(llm.chat(say("hi")), 5)


async def test_tool_calls_in_the_text_and_repaired_arguments(make_llm, llm_sim):
    llm = await make_llm()
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.hall"}
    llm_sim.script.append({"content_tool_call": {"name": "call_service", "arguments": args}})
    result = await llm.chat(say("hi"), TOOLS)
    assert result.tool_calls[0].arguments == args and "<tool_call>" not in result.text

    llm_sim.script.append(
        {"name": "call_service", "raw_arguments": '{"domain": "light", "service": "turn_on",}'}
    )
    result = await llm.chat(say("hi"), TOOLS)
    assert result.tool_calls[0].arguments == {"domain": "light", "service": "turn_on"}


async def test_a_cancelled_caller_frees_the_slot(make_llm, llm_sim):
    llm = await make_llm()
    first = await busy(llm, llm_sim, 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await asyncio.wait_for(llm.chat(say("next")), 3)
    assert result.text


async def test_stop_drops_queued_requests(make_llm, llm_sim):
    llm = await make_llm()
    in_flight = await busy(llm, llm_sim, 2)
    queued = asyncio.create_task(llm.chat(say("queued")))
    await asyncio.sleep(0.02)
    await llm.stop()
    with pytest.raises(LLMDropped):
        await queued
    with pytest.raises(LLMDropped):  # the caller of the request being served is not left hanging
        await asyncio.wait_for(in_flight, 1)
    with pytest.raises(LLMDropped):
        await llm.chat(say("after stop"))
