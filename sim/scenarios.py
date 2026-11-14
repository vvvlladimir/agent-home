"""Scripted situations for the sim panel. Each scenario is a function of the current house that
returns steps for `Life.play`: `{"entity", "state", "cause"}`, `{"wait": seconds}`.

Causes matter: `user` = the owner in the HA UI, `physical` = a wall switch or a sensor,
`automation` = one of the owner's automations. The habit finder and the agents read them.
"""

from __future__ import annotations

from collections.abc import Callable

from sim.fake_ha import HAState


def _have(ha: HAState, *eids: str) -> list[str]:
    return [e for e in eids if e in ha.states]


def _platform(ha: HAState, platform: str) -> list[str]:
    return [
        e["entity_id"]
        for e in ha.entity_registry
        if e.get("platform") == platform and e["entity_id"] in ha.states
    ]


def _owner_devices(ha: HAState) -> list[str]:
    return _have(ha, "device_tracker.iphone", "device_tracker.galaxy_s24", "device_tracker.watch")


def _set(eids: list[str], state: str, cause: str = "physical") -> list[dict]:
    return [{"entity": e, "state": state, "cause": cause} for e in eids]


def _outage(ha: HAState, eids: list[str], seconds: float) -> list[dict]:
    before = {e: ha.states[e]["state"] for e in eids}
    steps = _set(eids, "unavailable")
    steps.append({"wait": seconds})
    steps += [{"entity": e, "state": s, "cause": "physical"} for e, s in before.items()]
    return steps


def arrive_home(ha: HAState) -> list[dict]:
    return [
        *_set(_owner_devices(ha), "home"),
        {"wait": 20},
        {"entity": "person.owner", "state": "home"},
        {"wait": 15},
        {"entity": "lock.front_door", "state": "unlocked", "cause": "physical"},
        {"entity": "binary_sensor.front_door_contact", "state": "on"},
        {"wait": 3},
        {"entity": "binary_sensor.hallway_motion", "state": "on"},
        {"wait": 5},
        {"entity": "binary_sensor.front_door_contact", "state": "off"},
        {"wait": 25},
        {"entity": "binary_sensor.hallway_motion", "state": "off"},
        {"entity": "binary_sensor.living_room_motion", "state": "on"},
        {"wait": 10},
        {"entity": "light.living_room_lamp", "state": "on", "cause": "user"},
        {"wait": 30},
        {"entity": "binary_sensor.living_room_motion", "state": "off"},
    ]


def leave_home(ha: HAState) -> list[dict]:
    lights = [e for e, s in ha.states.items() if e.startswith("light.") and s["state"] == "on"]
    return [
        *_set(lights[:3], "off", "user"),
        {"entity": "binary_sensor.hallway_motion", "state": "on"},
        {"wait": 10},
        {"entity": "binary_sensor.front_door_contact", "state": "on"},
        {"wait": 4},
        {"entity": "binary_sensor.front_door_contact", "state": "off"},
        {"entity": "binary_sensor.hallway_motion", "state": "off"},
        {"wait": 30},
        {"entity": "lock.front_door", "state": "locked", "cause": "user"},
        *_set(_owner_devices(ha), "not_home"),
        {"wait": 60},
        {"entity": "person.owner", "state": "not_home"},
    ]


def evening(ha: HAState) -> list[dict]:
    return [
        {"entity": "sun.sun", "state": "below_horizon", "attributes": {"elevation": -4.2, "rising": False}},
        {"entity": "sensor.living_room_illuminance", "state": "8"},
        {"wait": 20},
        {"entity": "binary_sensor.living_room_motion", "state": "on"},
        {"entity": "light.living_room_lamp", "state": "on", "cause": "user"},
        {"wait": 10},
        {"entity": "media_player.living_room_tv", "state": "on", "cause": "user"},
        {
            "entity": "cover.living_room_blinds",
            "state": "closed",
            "cause": "automation",
            "automation": "automation.evening_blinds",
        },
        {"wait": 40},
        {"entity": "binary_sensor.living_room_motion", "state": "off"},
    ]


def night(ha: HAState) -> list[dict]:
    lights = [e for e, s in ha.states.items() if e.startswith("light.") and s["state"] == "on"]
    return [
        {"entity": "media_player.living_room_tv", "state": "off", "cause": "user"},
        *_set(lights, "off", "user"),
        {"entity": "light.bedroom_lamp", "state": "on", "cause": "user"},
        {"wait": 45},
        {"entity": "light.bedroom_lamp", "state": "off", "cause": "user"},
        {"entity": "alarm_control_panel.home", "state": "armed_home", "cause": "user"},
    ]


def window_while_heating(ha: HAState) -> list[dict]:
    return [
        {"entity": "climate.bedroom", "state": "heat", "attributes": {"hvac_action": "heating"}},
        {"entity": "binary_sensor.bedroom_window", "state": "on"},
        {"wait": 60},
        {"entity": "sensor.bedroom_temperature", "state": "18.1"},
        {"wait": 60},
        {"entity": "sensor.bedroom_temperature", "state": "17.2"},
        {"wait": 120},
        {"entity": "sensor.bedroom_temperature", "state": "16.4"},
    ]


def smoke(ha: HAState) -> list[dict]:
    det = [e for e in ha.states if e.startswith("binary_sensor.") and e.endswith("_smoke")]
    return [*_set(det[:1], "on"), {"wait": 90}, *_set(det[:1], "off")] if det else []


def water_leak(ha: HAState) -> list[dict]:
    det = [e for e in ha.states if e.startswith("binary_sensor.") and e.endswith("_water_leak")]
    return [*_set(det[:1], "on"), {"wait": 120}, *_set(det[:1], "off")] if det else []


def zigbee_outage(ha: HAState) -> list[dict]:
    """The coordinator drops: every Zigbee2MQTT entity goes unavailable for a minute, then returns."""
    return _outage(ha, _platform(ha, "mqtt"), 60)


def router_reboot(ha: HAState) -> list[dict]:
    """The router restarts: its sensors go unavailable, every client tracker drops and comes back."""
    eids = _platform(ha, "fritz")
    before = {e: ha.states[e]["state"] for e in eids}
    trackers = [e for e in eids if e.startswith("device_tracker.")]
    return [
        *_set([e for e in eids if e not in trackers], "unavailable"),
        *_set(trackers, "not_home"),
        {"wait": 90},
        *[{"entity": e, "state": s} for e, s in before.items()],
    ]


def presence_flapping(ha: HAState) -> list[dict]:
    """A phone on bad Wi-Fi: the tracker flaps home / not_home, person.owner stays home."""
    devs = _owner_devices(ha)[:1]
    steps: list[dict] = []
    for _ in range(6):
        steps += [*_set(devs, "not_home"), {"wait": 8}, *_set(devs, "home"), {"wait": 12}]
    return steps


SCENARIOS: dict[str, tuple[str, Callable[[HAState], list[dict]]]] = {
    "arrive_home": ("Owner comes home: phone, door, motion, a lamp", arrive_home),
    "leave_home": ("Owner leaves: lights off, door, lock, phone and person away", leave_home),
    "evening": ("Sunset, TV on, the 'Evening blinds' automation closes the blinds", evening),
    "night": ("Everything off, bedroom lamp, alarm armed", night),
    "window_while_heating": ("Bedroom window opens while the heating runs", window_while_heating),
    "smoke": ("A Zigbee smoke detector fires for 90 s (needs a generated house)", smoke),
    "water_leak": ("A Zigbee leak sensor fires for 2 min (needs a generated house)", water_leak),
    "zigbee_outage": ("Zigbee2MQTT drops: all its entities unavailable for 60 s", zigbee_outage),
    "router_reboot": ("Router restarts: its sensors and all trackers drop for 90 s", router_reboot),
    "presence_flapping": ("The owner's phone flaps home / not_home on bad Wi-Fi", presence_flapping),
}
