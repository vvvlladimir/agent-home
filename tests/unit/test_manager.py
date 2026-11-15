"""D27 house manager: a deterministic prefilter decides whether the model is called; everything it
does goes through the actuator (policy, shadow) or the draft lifecycle; caps and the kill switch hold."""

from __future__ import annotations

from types import SimpleNamespace

from app.config import RuntimeSettings
from app.core.council import MODE_ENTITY, ORCHESTRATOR
from app.core.lifecycle import Lifecycle
from app.core.manager import Manager
from app.core.orchestrator import Orchestrator
from tests.helpers import AutomationHA, call, make_stack


def stack(tmp_path, *script, **settings):
    runtime = RuntimeSettings(**{"manager_enabled": True, "manager_sweep_min": 60, **settings})
    s = make_stack(tmp_path, ha=AutomationHA(), settings=runtime)
    s.ha.add("light.hall", "on")  # no area: no room agent owns it, the orchestrator does
    s.ha.add("lock.front", "locked")  # also unowned, but T3
    s.ha.add("person.owner", "not_home")
    s.llm.steps = list(script)
    s.lifecycle = Lifecycle(s.store, s.feed, s.ha, s.safety, owner_of=s.council.ownership.owner_of)
    orch_settings = SimpleNamespace(orchestrator_provider="local", anthropic_api_key="", anthropic_model="m",
                                    anthropic_base_url="", openai_api_key="", openai_model="m", openai_base_url="",
                                    daily_budget_eur=1.0, redact_cloud=True)  # fmt: skip
    s.orch = Orchestrator(orch_settings, s.llm, s.store, s.feed, s.ha, s.safety, s.lifecycle, s.rt)
    s.orch.council = s.council
    s.rt.orchestrator = s.orch
    s.now = [1_000_000.0]
    s.manager = Manager(s.orch, s.council, s.actuator, None, runtime, now=lambda: s.now[0])
    s.council.manager = s.manager
    return s


def run_status(s, run_id: str) -> str:
    return s.store.query("SELECT status FROM runs WHERE id=?", (run_id,))[0]["status"]


def test_scope_is_the_mode_helper_plus_unowned_switchables_only_when_enabled(tmp_path):
    s = stack(tmp_path)
    assert "light.hall" in s.manager.owned() and "light.room_lamp" not in s.manager.owned()
    assert "lock.front" not in s.manager.owned()  # not a switchable domain
    assert set(s.council.agent_config(ORCHESTRATOR).entities) == {MODE_ENTITY, "light.hall"}
    s.manager.settings = s.council.settings = RuntimeSettings(manager_enabled=False)
    assert s.council.agent_config(ORCHESTRATOR).entities == [MODE_ENTITY]


def test_device_settings_and_pairing_switches_are_never_owned(tmp_path):
    """QA-008: permit-join and config switches (Fritz! internet access) are not the manager's."""
    s = stack(tmp_path)
    s.ha.add("switch.zigbee2mqtt_bridge_permit_join", "off")
    s.ha.add("switch.laptop_internet_access", "on")
    s.ha.registry.entity_meta["switch.laptop_internet_access"] = {"entity_category": "config"}
    owned = s.manager.owned()
    assert "light.hall" in owned
    assert (
        "switch.zigbee2mqtt_bridge_permit_join" not in owned and "switch.laptop_internet_access" not in owned
    )
    assert "no room agent" in s.manager.status()["owned_note"]


async def test_a_sweep_with_nothing_to_do_never_calls_the_model(tmp_path):
    s = stack(tmp_path)
    s.ha.states["light.hall"]["state"] = "off"
    s.ha.states["light.room_lamp"]["state"] = "off"
    s.now[0] += 3601
    await s.manager.tick()
    await s.manager.wait()
    assert s.llm.requests == [] and s.manager.anomalies() == []


async def test_lights_left_on_with_nobody_home_wake_it_and_its_call_runs_in_shadow(tmp_path):
    s = stack(tmp_path, call("call_service", domain="light", service="turn_off", entity_id="light.hall", data={}),
              {"text": "Switched the hall light off."})  # fmt: skip
    assert any("nobody is home" in a and "light.hall" in a for a in s.manager.anomalies())
    s.now[0] += 3601
    await s.manager.tick()
    await s.manager.wait()
    assert len(s.llm.requests) == 2
    [action] = s.store.query("SELECT agent_id, entity_ids, verdict FROM actions")
    assert action["agent_id"] == ORCHESTRATOR and "light.hall" in action["entity_ids"]
    assert action["verdict"] == "shadow"
    assert s.ha.calls == []  # shadow: nothing reached HA


async def test_a_manual_wake_sees_the_findings_and_an_off_manager_owns_nothing(tmp_path):
    """QA-003/006: a wake by hand carries what the prefilter found (lights on, nobody home); with
    the manager off the model is told it switches nothing."""
    s = stack(tmp_path, {"text": "Reported the lights."}, manager_enabled=False)
    assert s.manager.request("manual: look", force=True)["status"] == "started"
    await s.manager.wait()
    sent = str(s.llm.requests[0])
    assert "Found by the deterministic check" in sent and "nobody is home and still on" in sent
    assert "the manager is off, you switch nothing" in sent
    status = s.manager.status()
    assert status["last_run_id"] and "manual wake still runs it once" in status["owned_note"]


async def test_a_report_naming_an_entity_that_does_not_exist_is_sent_back(tmp_path):
    """QA-005: the inbox never names invented ids; the model gets the error and can fix it."""
    s = stack(tmp_path,
              call("report", severity="info", summary="person.person_1 is away, light.hall is on"),
              call("report", severity="info", summary="Nobody is home (person.owner); light.hall is on"),
              {"text": "Reported."})  # fmt: skip
    s.manager.request("manual: look", force=True)
    await s.manager.wait()
    inbox = [r["summary"] for r in s.store.query("SELECT summary FROM inbox")]
    assert inbox == ["Nobody is home (person.owner); light.hall is on"]
    assert "unknown entity ids person.person_1" in str(s.llm.requests[1])


async def test_it_cannot_act_on_a_room_agents_entity_or_a_t3_one(tmp_path):
    s = stack(tmp_path,
              call("call_service", domain="light", service="turn_off", entity_id="light.room_lamp", data={}),
              call("call_service", domain="lock", service="unlock", entity_id="lock.front", data={}),
              {"text": "Done."})  # fmt: skip
    s.manager.request("manual: test", force=True)
    await s.manager.wait()
    verdicts = [r["verdict"] for r in s.store.query("SELECT verdict FROM actions ORDER BY ts")]
    assert verdicts == ["denied", "denied"] and s.ha.calls == []


async def test_wakes_are_capped_per_hour_and_refused_with_the_kill_switch(tmp_path):
    s = stack(tmp_path, manager_max_wakes_per_hour=1)
    assert s.manager.request("first")["status"] == "started"
    await s.manager.wait()
    assert s.manager.request("second")["status"] == "refused"
    s.now[0] += 3601
    s.safety.set_kill(True, "test")
    assert s.manager.request("third")["detail"] == "the kill switch is engaged"


async def test_a_proposal_for_an_automation_that_is_not_ours_is_refused(tmp_path):
    s = stack(tmp_path, call("propose_automation", request="dim it", ha_id="1700000000001"), {"text": "Ok."})
    s.manager.request("manual: test", force=True)
    await s.manager.wait()
    [run] = s.store.query("SELECT trace FROM runs WHERE agent_id='orchestrator'")
    assert "is not one of our deployed automations" in run["trace"]
    assert s.store.query("SELECT COUNT(*) AS n FROM proposals")[0]["n"] == 0


async def test_an_escalation_without_an_owner_goes_to_the_manager(tmp_path):
    s = stack(tmp_path, {"text": "Noted."})
    result = s.council.escalate("room", "the hall light flickers", source="event")
    assert "house manager" in result["note"]
    await s.manager.wait()
    [run] = s.store.query("SELECT wake_reason, status FROM runs WHERE agent_id='orchestrator'")
    assert "the hall light flickers" in run["wake_reason"] and run["status"] == "ok"


def manager_runs(s) -> int:
    return s.store.query("SELECT COUNT(*) AS n FROM runs WHERE agent_id='orchestrator'")[0]["n"]


async def sweep(s) -> None:
    s.now[0] += 3601
    await s.manager.tick()
    await s.manager.wait()


async def test_the_same_finding_is_handed_to_the_model_once_until_it_changes(tmp_path):
    """Stories 2026-11-15: in shadow the lights it asked about stay on; every hourly sweep sent
    the same finding again and spent the day's cloud budget."""
    s = stack(tmp_path)
    await sweep(s)
    assert manager_runs(s) == 1
    await sweep(s)  # same lights still on (shadow): no model call
    assert manager_runs(s) == 1
    assert any("already handed over" in r["summary"] for r in s.store.query("SELECT summary FROM feed"))
    s.ha.add("light.porch", "on")  # one more light left on: new
    await sweep(s)
    assert manager_runs(s) == 2
    s.ha.states["light.hall"]["state"] = "off"  # cleared: nothing new to say
    await sweep(s)
    assert manager_runs(s) == 2
    s.ha.states["light.hall"]["state"] = "on"  # and back again: new
    await sweep(s)
    assert manager_runs(s) == 3


async def test_an_escalation_from_a_task_run_does_not_wake_the_manager_again(tmp_path):
    """The answer to its own task stays on the board for its next run (no manager → task →
    escalation → manager loop)."""
    s = stack(tmp_path)
    result = s.council.escalate("room", "the hall light is still on (shadow)", source="task")
    assert result["status"] == "on_board" and "next run" in result["note"]
    await s.manager.wait()
    assert manager_runs(s) == 0
    assert any("still on (shadow)" in r["text"] for r in s.council.board.since(0))


async def test_a_spent_cloud_budget_stops_wakes_and_tells_the_owner_once_a_day(tmp_path):
    s = stack(tmp_path)
    s.orch.settings.orchestrator_provider, s.orch.settings.anthropic_api_key = "anthropic", "key"
    s.orch.budget.add("anthropic", "m", 0, 0, 1.2)
    first = s.manager.request("sweep: lights")
    assert first["status"] == "refused" and "budget" in first["detail"]
    assert s.manager.request("manual: look", force=True)["status"] == "refused"
    await s.manager.wait()
    assert manager_runs(s) == 0 and s.llm.requests == []
    inbox = s.store.query("SELECT severity, summary FROM inbox")
    assert len(inbox) == 1 and inbox[0]["severity"] == "warning" and "budget" in inbox[0]["summary"]



async def test_a_failed_room_run_names_what_woke_it(tmp_path):
    """Stories 2026-11-15: the balcony agent timed out on "the fridge lost power"; the sweep said
    only "1 run timeout" and the fridge was lost."""
    s = stack(tmp_path)
    s.store.execute(
        "INSERT INTO runs (id, agent_id, status, wake_source, wake_reason, started, shadow) "
        "VALUES ('r1', 'room', 'timeout', 'event', 'switch.fridge changed on → off. A fridge lost power.', ?, 1)",
        (s.now[0] - 60,),
    )
    s.store.execute(
        "INSERT INTO runs (id, agent_id, status, wake_source, wake_reason, started, shadow) "
        "VALUES ('r2', 'room', 'timeout', 'cron', 'Scheduled check.', ?, 1)",
        (s.now[0] - 30,),
    )
    [line] = [a for a in s.manager.anomalies() if a.startswith("room agent room")]
    assert line == ("room agent room: 2 run(s) timeout, woken by: "
                    "switch.fridge changed on → off. A fridge lost power.")  # fmt: skip


async def test_in_shadow_the_manager_gives_no_tasks_and_cannot_claim_what_did_not_happen(tmp_path):
    """Stories 2026-11-15: in shadow every task came back as a "could not" warning (18 a day), and
    agents told the owner things were done or awaited approval (QA-007)."""
    s = stack(tmp_path,
              call("call_service", domain="light", service="turn_off", entity_id="light.hall", data={}),
              call("report", severity="info", summary="I turned off light.hall."),
              call("report", severity="info", summary="I would switch off light.hall; nothing was changed."),
              {"text": "Reported."})  # fmt: skip
    s.manager.request("manual: look", force=True)
    await s.manager.wait()
    first = s.llm.requests[0]
    assert "assign_task" not in str(first["tools"]) and "Shadow mode: you give no tasks" in str(first)
    assert "this run's actions were only recorded" in str(s.llm.requests[2])
    assert [r["summary"] for r in s.store.query("SELECT summary FROM inbox")] == [
        "I would switch off light.hall; nothing was changed."]  # fmt: skip
