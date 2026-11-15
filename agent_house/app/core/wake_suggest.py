"""Wake rules suggested for a room agent from the devices in its room (deterministic, no model).

The add-on ships no wake rules: every agent starts with what the owner writes. The stories run of
2026-11-15 showed what a hands-off owner then misses: the front door unlocked at night, the garage
open, a leak sensor's battery running out. Creating an agent now offers rules for the room's own
entities, one rule per kind, each with its reason; the owner sees them in the editor and keeps,
changes or removes them before saving. Nothing is applied without the owner.

Kinds, from the entity's domain, device class and id (entity ids are exact, never globs, so a rule
only ever names what the room has):
- alarm: smoke, gas, carbon monoxide, water leak → on (priority alarm, no debounce);
- opening: doors, windows, contacts → opened;
- lock: unlocked, open or jammed (locks are T3: the agent reports, it never acts);
- garage: a garage door or gate cover → opening / open (T3 as well);
- presence: motion, occupancy → on, with a longer cooldown;
- cold: room temperature below `COLD_C`, at most every 3 h; not for outdoor areas (balcony, garden, …);
- battery: low-battery binary sensors → on, at most once a day;
- power: a fridge or freezer switch → off or unavailable;
- problem: binary sensors of class `problem` → on.
"""

from __future__ import annotations

import re

from .agents import _device_class

COLD_C = 16.0
ALARM_CLASSES = frozenset({"smoke", "gas", "carbon_monoxide", "moisture", "safety"})
ALARM_ID = re.compile(r"_(smoke|gas|carbon_monoxide|co|water_leak|leak|flood)$")
OPENING_CLASSES = frozenset({"door", "window", "opening", "garage_door"})
OPENING_ID = re.compile(r"_(door|window|contact|opening)(_\d+)?$")
PRESENCE_CLASSES = frozenset({"motion", "occupancy", "presence"})
PRESENCE_ID = re.compile(r"_(motion|occupancy|presence)(_\d+)?$")
BATTERY_ID = re.compile(r"_battery_low$")
GARAGE_ID = re.compile(r"garage|gate")
KEEP_POWERED = re.compile(r"fridge|freezer|refrigerator")
DEVICE_TEMPERATURE = re.compile(r"_(device|cpu|chip|board)_temperature$")
OUTDOOR = re.compile(r"balcon|terrace|garden|patio|yard|outdoor|outside|porch|garage|loggia|shed", re.I)

# kind → (rule fields without `entities`, in the order the editor shows them)
KINDS: dict[str, dict] = {
    "alarm": {"to": ["on"], "debounce_s": 0, "cooldown_s": 30, "priority": "alarm",
              "reason": "Smoke, gas or water leak."},
    "lock": {"to": ["unlocked", "open", "jammed"], "debounce_s": 2, "cooldown_s": 300,
             "reason": "Lock unlocked or jammed: is that expected now? Report, never act (T3)."},
    "garage": {"to": ["opening", "open"], "debounce_s": 5, "cooldown_s": 600,
               "reason": "Garage door or gate opened: is that expected now? Report, never act (T3)."},
    "opening": {"to": ["on"], "debounce_s": 2, "cooldown_s": 120, "reason": "Door or window opened."},
    "power": {"to": ["off", "unavailable"], "debounce_s": 60, "cooldown_s": 3600,
              "reason": "A fridge or freezer lost power."},
    "problem": {"to": ["on"], "debounce_s": 30, "cooldown_s": 3600, "reason": "A device reports a problem."},
    "cold": {"below": COLD_C, "debounce_s": 300, "cooldown_s": 10800,
             "reason": f"Room got cold (below {COLD_C:g} °C)."},
    "battery": {"to": ["on"], "debounce_s": 60, "cooldown_s": 86400,
                "reason": "A device's battery is low: tell the owner, once a day at most."},
    "presence": {"to": ["on"], "debounce_s": 1, "cooldown_s": 300, "reason": "Motion in the room."},
}  # fmt: skip


def kind_of(ha, entity_id: str) -> str | None:
    """Which suggested rule an entity belongs to, or None."""
    domain = entity_id.split(".", 1)[0]
    cls = _device_class(ha, entity_id)
    name = str(((ha.states.get(entity_id) or {}).get("attributes") or {}).get("friendly_name") or "").lower()
    if domain == "lock":
        return "lock"
    if domain == "cover":
        return "garage" if cls in ("garage", "gate") or GARAGE_ID.search(entity_id) else None
    if domain == "switch":
        return "power" if KEEP_POWERED.search(entity_id) or KEEP_POWERED.search(name) else None
    if domain == "sensor":
        is_temp = cls == "temperature" or entity_id.endswith("_temperature")
        return "cold" if is_temp and not DEVICE_TEMPERATURE.search(entity_id) else None
    if domain != "binary_sensor":
        return None
    if cls in ALARM_CLASSES or (cls is None and ALARM_ID.search(entity_id)):
        return "alarm"
    if cls == "battery" or BATTERY_ID.search(entity_id):
        return "battery"
    if cls == "problem":
        return "problem"
    if cls in OPENING_CLASSES or (cls is None and OPENING_ID.search(entity_id)):
        return "opening"
    if cls in PRESENCE_CLASSES or (cls is None and PRESENCE_ID.search(entity_id)):
        return "presence"
    return None


def suggest(ha, room: str) -> list[dict]:
    """Event rules for the entities of `room`, one per kind, in `KINDS` order. Hidden and disabled
    entities and device settings are left out; diagnostic low-battery sensors are kept (a wake
    rule naming them puts them in the agent's scope, see `agents.resolve_scope`)."""
    registry = ha.registry
    area_name = str((registry.areas.get(room) or {}).get("name") or "")
    outdoor = bool(OUTDOOR.search(room) or OUTDOOR.search(area_name))
    found: dict[str, list[str]] = {}
    for entity_id in sorted(registry.entities_in(room)):
        if entity_id not in ha.states or registry.is_hidden(entity_id):
            continue
        category = registry.entity_meta.get(entity_id, {}).get("entity_category")
        kind = kind_of(ha, entity_id)
        if kind is None or category == "config" or (category and kind != "battery"):
            continue
        if kind == "cold" and outdoor:  # cold on the balcony in winter is the weather, not news
            continue
        found.setdefault(kind, []).append(entity_id)
    return [{"entities": found[kind], **fields} for kind, fields in KINDS.items() if kind in found]
