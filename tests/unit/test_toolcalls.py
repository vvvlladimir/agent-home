"""Tool-call parsing: fix syntax, never guess a value."""

from __future__ import annotations

import json

import pytest

from app.core.toolcalls import extract_tool_calls, loads_lenient, parse_arguments, visible_text
from app.core.wire import StreamAccumulator, parse_completion, read_timings

KNOWN = ["call_service", "report", "get_state"]
ARGS = {"domain": "light", "service": "turn_on", "entity_id": "light.hallway"}


@pytest.mark.parametrize(
    "raw",
    [
        '{"domain": "light", "service": "turn_on", "entity_id": "light.hallway",}',  # trailing comma
        "{'domain': 'light', 'service': 'turn_on', 'entity_id': 'light.hallway'}",  # Python literal
        '```json\n{"domain": "light", "service": "turn_on", "entity_id": "light.hallway"}\n```',
        'Sure: {"domain": "light", "service": "turn_on", "entity_id": "light.hallway"} done',
        '{"domain": "light", "service": "turn_on", "entity_id": "light.hallway"',  # closing brace missing
        json.dumps(json.dumps(ARGS)),  # double-encoded
    ],
)
def test_syntax_errors_are_repaired(raw):
    args, repaired, error = parse_arguments(raw)
    assert error is None and repaired is True and args == ARGS


@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        '{"entity_id": "light.hall',  # cut inside a string: completing it would invent an entity id
        '{"data": {"brightness_pct": 3',  # cut inside a number: 3 or 30?
        '{"entity_id": "light.hallway", "service"',  # a key without a value
        '{"entity_id": "light.hallway", "service":',
        "[1, 2, 3]",  # not an object
        '"just a string"',
        "{'a': {1, 2}}",  # a Python set is not JSON
        "__import__('os').system('true')",
    ],
)
def test_ambiguous_or_truncated_arguments_are_an_error_not_a_guess(raw):
    args, _, error = parse_arguments(raw)
    assert error and args == {}


def test_output_cut_by_the_token_limit_is_never_closed():
    raw = '{"domain": "light", "service": "turn_on", "entity_id": "light.hallway"'
    assert parse_arguments(raw)[2] is None
    assert parse_arguments(raw, truncated=True)[2]
    message = {"tool_calls": [{"id": "a", "function": {"name": "call_service", "arguments": raw}}]}
    assert extract_tool_calls(message, "length", KNOWN)[0].error
    assert extract_tool_calls(message, "tool_calls", KNOWN)[0].arguments == ARGS


def test_valid_json_is_untouched_and_braces_inside_strings_do_not_confuse_the_scanner():
    assert loads_lenient('{"a": 1}') == ({"a": 1}, False)
    value, repaired = loads_lenient('{"summary": "use } and , ] freely", "n": [1, 2,],}')
    assert repaired and value == {"summary": "use } and , ] freely", "n": [1, 2]}
    assert parse_arguments(None) == ({}, False, None) and parse_arguments("") == ({}, False, None)
    assert parse_arguments({"a": 1}) == ({"a": 1}, False, None)
    assert parse_arguments(5)[2]


def test_native_calls():
    message = {
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "call_service", "arguments": json.dumps(ARGS)}},
            {"id": "c2", "type": "function", "function": {"name": "report", "arguments": {"summary": "x"}}},
            {"id": "c3", "type": "function", "function": {"name": "format_disk", "arguments": "{}"}},
            {"id": "c4", "type": "function", "function": {"name": "report", "arguments": "{nope"}},
        ],
    }
    one, two, three, four = extract_tool_calls(message, "tool_calls", KNOWN)
    assert (one.name, one.arguments, one.repaired, one.error, one.source) == ("call_service", ARGS, False, None, "native")
    assert two.arguments == {"summary": "x"}
    assert three.error == "unknown tool 'format_disk'" and three.arguments == {}
    assert "not valid JSON" in four.error
    assert json.loads(one.as_message_entry()["function"]["arguments"]) == ARGS


@pytest.mark.parametrize(
    "content",
    [
        f'<tool_call>{json.dumps({"name": "call_service", "arguments": ARGS})}</tool_call>',
        f'<think>hmm, the light</think>\n<tool_call>\n{json.dumps({"name": "call_service", "arguments": ARGS})}\n</tool_call>',
        f'<tool_call>{json.dumps({"name": "call_service", "arguments": ARGS})}',  # closing tag missing
        f'<tool_call>{json.dumps({"name": "call_service", "parameters": ARGS})}</tool_call>',
        f'<tool_call>{json.dumps({"name": "call_service", "arguments": json.dumps(ARGS)})}</tool_call>',
        f"<function=call_service>{json.dumps(ARGS)}</function>",
        "<tool_call><function=call_service><parameter=domain>light</parameter><parameter=service>turn_on"
        "</parameter><parameter=entity_id>light.hallway</parameter></function></tool_call>",
        f'```json\n{json.dumps({"name": "call_service", "arguments": ARGS})}\n```',
        json.dumps({"name": "call_service", "arguments": ARGS}),
    ],
)
def test_calls_embedded_in_the_text(content):
    calls = extract_tool_calls({"role": "assistant", "content": content}, "stop", KNOWN)
    assert len(calls) == 1
    assert (calls[0].name, calls[0].arguments, calls[0].error, calls[0].source) == ("call_service", ARGS, None, "content")
    assert visible_text({"content": content}).count("tool_call") == 0


def test_plain_text_and_unmarked_json_for_unknown_tools_are_not_calls():
    assert extract_tool_calls({"content": "The light is on. Nothing to do."}, "stop", KNOWN) == []
    assert extract_tool_calls({"content": '{"name": "Alice", "arguments": {"a": 1}}'}, "stop", KNOWN) == []
    assert extract_tool_calls({"content": '{"temperature": 21}'}, "stop", KNOWN) == []
    # explicit markers with an unknown tool or broken JSON are reported, not dropped
    unknown = extract_tool_calls({"content": '<tool_call>{"name": "rm", "arguments": {}}</tool_call>'}, "stop", KNOWN)
    assert unknown[0].error == "unknown tool 'rm'"
    broken = extract_tool_calls({"content": "<tool_call>{nope</tool_call>"}, "stop", KNOWN)
    assert broken[0].error
    two = extract_tool_calls(
        {"content": '<tool_call>{"name": "report", "arguments": {"summary": "a"}}</tool_call>'
                    '<tool_call>{"name": "get_state", "arguments": {"entity_id": "light.x"}}</tool_call>'},
        "stop",
        KNOWN,
    )
    assert [c.name for c in two] == ["report", "get_state"] and two[0].id != two[1].id
    assert visible_text({"content": "<think>secret plan</think>Done."}) == "Done."
    assert visible_text({"content": "<think>cut off"}) == ""


def test_stream_accumulator_and_timings():
    stream = StreamAccumulator()
    lines = [
        ": comment",
        'data: {"model": "m", "choices": [{"delta": {"role": "assistant", "content": ""}}]}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "report", "arguments": "{\\"sum"}}]}}]}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "mary\\": \\"x\\"}"}}]}}]}',
        'data: {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}',
        "data: not json",
        'data: {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 7}, "timings": {"prompt_n": 40, "prompt_ms": 100.0, "predicted_n": 7, "predicted_per_second": 20.0}}',
        "data: [DONE]",
    ]
    produced = [stream.feed_line(line) for line in lines]
    assert produced == [False, False, True, True, False, False, False, False] and stream.done
    result = stream.result()
    assert result["finish_reason"] == "tool_calls" and result["model"] == "m"
    call = extract_tool_calls(result["message"], result["finish_reason"], ["report"])[0]
    assert call.arguments == {"summary": "x"} and call.id == "c1"
    timings = read_timings(result["timings"], result["usage"])
    assert timings["prefill_tps"] == 400.0 and timings["decode_tps"] == 20.0 and timings["prompt_tokens"] == 50
    assert read_timings(None)["decode_tps"] is None and read_timings({"prompt_n": "x"}, [])["prompt_n"] is None
    failed = StreamAccumulator()
    failed.feed_line('data: {"error": {"message": "slot unavailable"}}')
    assert failed.error == "slot unavailable"
    with pytest.raises(ValueError):
        parse_completion({"choices": []})
    assert parse_completion({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})["message"]["content"] == "hi"
