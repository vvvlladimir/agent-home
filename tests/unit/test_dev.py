"""The developer snapshot masks a key wherever an upstream error text echoes it."""

from app.api.dev import _scrub


def test_a_key_echoed_in_an_error_text_is_masked():
    snap = {
        "orchestrator": {"last_calls": [{"error": "401: invalid x-api-key sk-ant-very-secret"}]},
        "ha": {"last_error": "auth failed for token abc-very-secret", "connected": False, "n": 3},
    }
    out = _scrub(snap, ["sk-ant-very-secret", "abc-very-secret"])
    assert "very-secret" not in str(out)
    assert out["orchestrator"]["last_calls"][0]["error"] == "401: invalid x-api-key ***"
    assert out["ha"]["connected"] is False and out["ha"]["n"] == 3
