"""What kind of entity an id is, as the orchestrator, the house manager and the habit scan read
it: device bookkeeping (noise), sensors that describe a room, network pairing switches nothing of
ours may own."""

from __future__ import annotations

import re

from .policy import READ_ONLY_DOMAINS

# Network-level switches nothing of ours should flip or learn from: opening the Zigbee / Z-Wave /
# Thread network to new devices. Usually `entity_category: config` too, but not always.
NEVER_OWNED = re.compile(r"_(permit_join|permit_joining|inclusion_mode|commissioning)$")
# Noise in a real HA: device bookkeeping, nothing the orchestrator or the learner reasons about.
NOISE_DOMAINS = frozenset({"update", "device_tracker"})
NOISE_DEVICE_CLASSES = frozenset({"battery", "signal_strength", "voltage", "connectivity", "update"})
NOISE_SUFFIX = re.compile(
    r"_(linkquality|rssi|signal|signal_strength|last_seen|uptime|firmware|update_state|battery|"
    r"battery_low|voltage|power_on_behavior|identify)$"
)
# Sensors that describe the room itself come before the other read-only entities.
ROOM_SENSOR_CLASSES = frozenset(
    {"motion", "occupancy", "presence", "door", "window", "opening", "temperature", "humidity",
     "illuminance", "smoke", "moisture", "gas", "carbon_monoxide"}
)  # fmt: skip


def _device_class(ha, entity_id: str) -> str | None:
    attrs = (ha.states.get(entity_id) or {}).get("attributes") or {}
    meta = ha.registry.entity_meta.get(entity_id, {})
    return attrs.get("device_class") or meta.get("device_class") or meta.get("original_device_class")


def is_noise(ha, entity_id: str) -> bool:
    """Device bookkeeping (battery, signal, firmware, …) that only fills a prompt."""
    domain = entity_id.split(".", 1)[0]
    if domain in NOISE_DOMAINS or NOISE_SUFFIX.search(entity_id):
        return True
    return domain in READ_ONLY_DOMAINS and _device_class(ha, entity_id) in NOISE_DEVICE_CLASSES


ROOM_SENSOR_SUFFIX = re.compile(
    r"_(motion|occupancy|presence|door|window|contact|temperature|humidity|illuminance|smoke|leak)$"
)


def is_room_sensor(ha, entity_id: str) -> bool:
    return entity_id.split(".", 1)[0] in READ_ONLY_DOMAINS and (
        _device_class(ha, entity_id) in ROOM_SENSOR_CLASSES or bool(ROOM_SENSOR_SUFFIX.search(entity_id))
    )
