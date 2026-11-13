"""Cloud prompts carry no personal data; the daily budget stops before it is crossed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.orchestrator import Budget, BudgetExhausted, price_eur
from app.core.redact import Redactor
from app.store.db import Store
from tests.helpers import StubHA


def test_redaction_removes_personal_data_and_keeps_the_house_readable():
    ha = StubHA([
        {"entity_id": "person.anna", "state": "home", "name": "Anna Schmidt"},
        {"entity_id": "zone.work", "state": "0", "name": "Bosch Campus"},
        {"entity_id": "light.room_lamp", "state": "off"},
    ])  # fmt: skip
    ha.location_name = "Haus Schmidt"
    redactor = Redactor.for_home(ha)
    text = (
        "person.anna (Anna Schmidt) left Bosch Campus (zone.work) at 52.5200, 13.4050, latitude: 52.52; "
        "Haus Schmidt router 192.168.1.1, fe80::1:2:3:4, aa:bb:cc:dd:ee:ff, https://cam.local/x?t=1, "
        "call +49 30 1234567 or 030 1234567, mail anna@example.com, key sk-ant-api03-abcdefgh, "
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U, "
        "light.room_lamp is off at 2026-11-13 10:41, binary_sensor.esphome_radar_bedroom_occupancy_ld241x, zone.home, "
        "light.anna_bedroom and sensor.schmidt_phone_battery"
    )
    out = redactor.redact(text)
    for leaked in ("anna", "Anna", "Schmidt", "Bosch", "52.52", "13.40", "192.168", "fe80", "aa:bb", "cam.local",
                   "1234567", "example.com", "sk-ant", "eyJ"):  # fmt: skip
        assert leaked not in out, (leaked, out)
    for kept in ("light.room_lamp is off at 2026-11-13 10:41", "binary_sensor.esphome_radar_bedroom_occupancy_ld241x",
                 "zone.home", "person.person_1", "zone.zone_1", "light.light_1", "sensor.sensor_1"):  # fmt: skip
        assert kept in out, (kept, out)
    answer = {"yaml": "entity_id: person.person_1\nzone: zone.zone_1", "list": ["person.person_1"]}
    assert redactor.restore(answer) == {
        "yaml": "entity_id: person.anna\nzone: zone.work",
        "list": ["person.anna"],
    }


def test_aliases_do_not_collide():
    redactor = Redactor()
    for n in range(1, 12):
        redactor.redact(f"person.p{n}")
    assert (
        redactor.restore("person.person_1 person.person_10 person.person_11")
        == "person.p1 person.p10 person.p11"
    )


def test_the_budget_is_a_hard_stop():
    budget = Budget(Store(":memory:"), SimpleNamespace(daily_budget_eur=0.01))
    assert budget.today() == {"daily_limit_eur": 0.01, "spent_today_eur": 0.0, "tokens_in_today": 0,
                              "tokens_out_today": 0, "exhausted": False}  # fmt: skip
    budget.check(0.009)
    budget.add("anthropic", "claude-haiku-5-5", 1000, 100, 0.008)
    budget.add("anthropic", "claude-haiku-5-5", 1000, 100, 0.001)
    budget.add("local", "qwen", 99999, 9999, 0.0)  # local tokens are free and not counted
    assert budget.today()["tokens_in_today"] == 2000 and budget.today()["spent_today_eur"] == pytest.approx(
        0.009
    )
    with pytest.raises(BudgetExhausted):
        budget.check(0.002)
    budget.add("anthropic", "claude-haiku-5-5", 1, 1, 0.001)
    assert budget.today()["exhausted"]
    assert Budget(Store(":memory:"), SimpleNamespace(daily_budget_eur=0)).today()["exhausted"], (
        "0 = cloud off"
    )


def test_prices():
    assert price_eur("claude-haiku-5-5", 1e6, 0) == pytest.approx(0.092)
    assert price_eur("claude-made-up", 1e6, 0) > price_eur("claude-opus-5-5", 1e6, 0), "unknown = expensive"


def test_a_reservation_holds_the_worst_case_until_the_call_is_settled():
    budget = Budget(Store(":memory:"), SimpleNamespace(daily_budget_eur=0.01))
    budget.reserve(0.006)
    with pytest.raises(BudgetExhausted):
        budget.reserve(0.006)  # a concurrent draft cannot pass the check too
    budget.add("anthropic", "claude-haiku-5-5", 10, 10, 0.001)
    budget.release(0.006)
    assert budget.today()["spent_today_eur"] == pytest.approx(0.001)
