"""D35: unusual states — a device in a state it is almost never in at this time, with nobody home."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core import predict
from app.core.insights import Change
from app.core.unusual import Unusual
from tests.unit.predict_house import at, house, household


class Inbox:
    def __init__(self) -> None:
        self.notes: list[tuple] = []

    def inbox_add(self, agent_id, run_id, severity, summary, dry=False, entity_id=None) -> None:
        self.notes.append((agent_id, severity, summary, entity_id))


class Store(dict):
    def set(self, key, value) -> None:
        self[key] = value


def with_people(changes):
    """The owner home from 17:00 to 08:00, out in between."""
    out = list(changes)
    for d in range(14):
        out += [Change(at(d, 8), "person.owner", "not_home", "physical"),
                Change(at(d, 17), "person.owner", "home", "physical")]  # fmt: skip
    return sorted(out, key=lambda c: c.ts)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def table():
    return predict.usual_states(with_people(household()), house(), days=14, min_days=3)


def test_a_lamp_on_at_three_at_night_is_noted_once():
    usual = table()
    night = predict.cell(at(14, 3), True)  # asleep at home
    assert usual["light.lamp"].get("on", {}).get(night, [0, 0])[0] == 0
    assert usual["light.lamp"]["off"][night][1] >= 600
    ha, now = house(), at(14, 3)
    ha.states["person.owner"] = {"state": "home"}
    ha.states["light.lamp"] = {
        "state": "on",
        "last_changed": iso(now - 3600),
        "attributes": {"friendly_name": "Lamp"},
    }
    store, inbox = Store(usual_states=usual), Inbox()
    unusual = Unusual(store, ha, inbox)
    assert unusual.check(now) == ["light.lamp"]
    [(agent, severity, text, entity)] = inbox.notes
    assert agent == "house" and severity == "info" and entity == "light.lamp"
    assert "Unusual: Lamp" in text and "while somebody is home" in text and "Nothing was switched" in text
    assert unusual.check(now + 600) == [], "once per stretch"
    ha.states["light.lamp"]["last_changed"] = iso(now - 600)
    assert Unusual(store, ha, Inbox()).check(now) == [], "not yet: on for ten minutes only"
    ha.states["person.owner"] = {"state": "not_home"}
    ha.states["light.lamp"]["last_changed"] = iso(now - 3600)
    assert Unusual(store, ha, Inbox()).check(now) == [], "never seen at night with nobody home: no basis"


def test_a_lamp_on_in_the_evening_with_someone_home_is_usual():
    ha, now = house(), at(14, 21)
    ha.states["person.owner"] = {"state": "home"}
    ha.states["light.lamp"] = {"state": "on", "last_changed": iso(now - 3600)}
    assert Unusual(Store(usual_states=table()), ha, Inbox()).check(now) == []
