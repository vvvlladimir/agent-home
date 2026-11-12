import json

import httpx
import pytest

from sim.fake_anthropic import create_app as anthropic_app
from sim.fake_llm import LLMSim, create_app
from sim.server import ServerThread

TOOLS = [
    {"type": "function", "function": {"name": "call_service"}},
    {"type": "function", "function": {"name": "report"}},
]


@pytest.fixture
def llm():
    sim = LLMSim()
    with ServerThread(create_app(sim)) as srv:
        srv.sim = sim
        yield srv


def chat(srv, content="hi", **kw):
    body = {"messages": [{"role": "user", "content": content}], **kw}
    return httpx.post(f"{srv.url}/v1/chat/completions", json=body, timeout=10)


def test_health_props_models_loading(llm):
    assert httpx.get(f"{llm.url}/health").json() == {"status": "ok"}
    assert httpx.get(f"{llm.url}/props").json()["default_generation_settings"]["n_ctx"] == 8192
    assert httpx.get(f"{llm.url}/v1/models").json()["data"]
    httpx.post(f"{llm.url}/_sim/loading", json={"loading": True})
    r = httpx.get(f"{llm.url}/health")
    assert r.status_code == 503 and r.json()["error"]["type"] == "unavailable_error"
    assert chat(llm).status_code == 503
    httpx.post(f"{llm.url}/_sim/loading", json={"loading": False})
    assert httpx.get(f"{llm.url}/health").status_code == 200


def test_text_usage_and_default_policy(llm):
    d = chat(llm).json()
    assert d["choices"][0]["message"]["content"] == "OK."
    assert d["usage"]["total_tokens"] == d["usage"]["prompt_tokens"] + d["usage"]["completion_tokens"]
    assert d["timings"]["predicted_per_second"] == 20.0 and "cache_n" in d["timings"]
    msg = "binary_sensor.hallway_motion is on. light.hallway is off."
    tc = chat(llm, msg, tools=TOOLS).json()["choices"][0]
    assert tc["finish_reason"] == "tool_calls"
    fn = tc["message"]["tool_calls"][0]["function"]
    assert fn["name"] == "call_service" and json.loads(fn["arguments"])["entity_id"] == "light.hallway"
    rep = chat(llm, "quiet", tools=TOOLS).json()["choices"][0]["message"]["tool_calls"][0]["function"]
    assert rep["name"] == "report" and json.loads(rep["arguments"])["severity"] == "info"
    body = {"messages": [{"role": "tool", "content": "{}", "tool_call_id": "x"}], "tools": TOOLS}
    r = httpx.post(f"{llm.url}/v1/chat/completions", json=body).json()
    assert r["choices"][0]["message"]["content"] == "Done."
    assert len(llm.sim.requests) == 4


def test_script_steps(llm):
    steps = [
        {"text": "hello"},
        {"tool_calls": [{"name": "report", "arguments": {"summary": "s"}}]},
        {"raw_arguments": "{broken", "name": "report"},
        {"content_tool_call": {"name": "report", "arguments": {"a": 1}}},
        {"garbage": True},
        {"status": 500},
    ]
    httpx.post(f"{llm.url}/_sim/script", json=steps)
    assert chat(llm).json()["choices"][0]["message"]["content"] == "hello"
    c = chat(llm).json()["choices"][0]["message"]["tool_calls"][0]
    assert c["type"] == "function" and json.loads(c["function"]["arguments"]) == {"summary": "s"}
    assert chat(llm).json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == "{broken"
    m = chat(llm).json()["choices"][0]
    assert "tool_calls" not in m["message"] and m["message"]["content"].startswith("<tool_call>")
    g = chat(llm)
    assert g.status_code == 200
    with pytest.raises(ValueError):
        g.json()
    assert chat(llm).status_code == 500
    assert httpx.get(f"{llm.url}/_sim/requests").json()[0]["messages"][0]["content"] == "hi"
    httpx.post(f"{llm.url}/_sim/reset")
    assert llm.sim.requests == [] and llm.sim.script == []


def test_hang_times_out(llm):
    httpx.post(f"{llm.url}/_sim/script", json=[{"hang": 2}])
    with pytest.raises(httpx.ReadTimeout):
        httpx.post(f"{llm.url}/v1/chat/completions", json={"messages": []}, timeout=0.3)


def test_stream(llm):
    httpx.post(
        f"{llm.url}/_sim/script",
        json=[{"text": "one two three"}, {"tool_calls": [{"name": "report", "arguments": {"x": 1}}]}],
    )
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
    with httpx.stream("POST", f"{llm.url}/v1/chat/completions", json=body) as r:
        lines = [x for x in r.iter_lines() if x]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(x[6:]) for x in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "one two three"
    assert chunks[-1]["usage"]["completion_tokens"] and chunks[-1]["timings"]["predicted_n"]
    with httpx.stream("POST", f"{llm.url}/v1/chat/completions", json=body) as r:
        chunks = [json.loads(x[6:]) for x in r.iter_lines() if x.startswith("data: {")]
    args = "".join(
        tc["function"].get("arguments", "")
        for c in chunks
        if c["choices"]
        for tc in c["choices"][0]["delta"].get("tool_calls", [])
    )
    assert json.loads(args) == {"x": 1}
    assert [c["choices"][0]["finish_reason"] for c in chunks if c["choices"]][-1] == "tool_calls"


# ---- anthropic ----------------------------------------------------------------------------
@pytest.fixture
def ant():
    with ServerThread(anthropic_app()) as srv:
        yield srv


def msg(srv, key="sk-ant-sim", path="/v1/messages", **kw):
    body = {
        "model": "claude-sonnet-5-5",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
        **kw,
    }
    return httpx.post(
        srv.url + path, json=body, headers={"x-api-key": key, "anthropic-beta": "b1"}, timeout=10
    )


def test_anthropic_basic_and_auth(ant):
    r = msg(ant, path="/v1/messages?beta=true")
    d = r.json()
    assert d["content"] == [{"type": "text", "text": "OK."}] and d["stop_reason"] == "end_turn"
    assert d["model"] == "claude-sonnet-5-5" and d["usage"]["cache_read_input_tokens"] == 0
    r = msg(ant, key="bad")
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
    rec = httpx.get(f"{ant.url}/_sim/requests").json()[0]
    assert rec["headers"]["anthropic-beta"] == "b1" and rec["query"] == {"beta": "true"}


@pytest.mark.parametrize(
    "extra",
    [
        {"temperature": 0},
        {"top_p": 1},
        {"top_k": 5},
        {"thinking": {"type": "enabled", "budget_tokens": 1000}},
        {"thinking": {"type": "disabled"}},
        {"tool_choice": {"type": "any"}},
        {"tool_choice": {"type": "tool", "name": "x"}},
    ],
)
def test_anthropic_rejects(ant, extra):
    r = msg(ant, **extra)
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"


def test_anthropic_accepts_modern_params(ant):
    assert msg(ant, thinking={"type": "adaptive"}, tool_choice={"type": "auto"}).status_code == 200


def test_anthropic_script(ant):
    httpx.post(
        f"{ant.url}/_sim/script",
        json=[
            {"tool_use": [{"name": "report", "input": {"a": 1}}]},
            {"status": 429},
            {"status": 529},
            {"status": 500},
            {"refusal": True},
            {"hang": 2},
        ],
    )
    d = msg(ant).json()
    assert d["stop_reason"] == "tool_use" and d["content"][0]["id"].startswith("toolu_sim_")
    assert d["content"][0]["input"] == {"a": 1}
    assert [msg(ant).json()["error"]["type"] for _ in range(3)] == [
        "rate_limit_error",
        "overloaded_error",
        "api_error",
    ]
    assert msg(ant).json()["stop_reason"] == "refusal"
    with pytest.raises(httpx.ReadTimeout):
        httpx.post(
            f"{ant.url}/v1/messages", json={"model": "m"}, headers={"x-api-key": "sk-ant-sim"}, timeout=0.3
        )
