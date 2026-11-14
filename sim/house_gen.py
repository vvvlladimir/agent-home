"""Grow the fake HA into a house that looks like a real one: Zigbee2MQTT devices, a router with its
client trackers, Home Assistant's own housekeeping entities, and the junk every real install
collects (unrenamed IEEE names, re-paired `_2` duplicates, orphans, disabled diagnostics, dead
batteries).

Deterministic: the same `HouseSpec` (seed included) always builds the same house. The 28-entity
base house of `fake_ha.HAState.default()` stays untouched; generated entities are tracked in
`ha.generated` and replaced on every rebuild.

Every generated state carries a `Stream` (how it changes over time), used by `sim/life.py`.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sim.fake_ha import HAState

PRESETS: dict[str, dict] = {
    "minimal": {"zigbee_devices": 0, "router_clients": 0, "junk_percent": 0, "core_extras": False},
    "realistic": {"zigbee_devices": 28, "router_clients": 30, "junk_percent": 15, "core_extras": True},
    "large": {"zigbee_devices": 160, "router_clients": 120, "junk_percent": 25, "core_extras": True},
}
EXTRA_AREAS = [
    ("bathroom", "Bathroom"),
    ("office", "Office"),
    ("balcony", "Balcony"),
    ("kids_room", "Kids Room"),
]
BASE_AREAS = ["kitchen", "living_room", "bedroom", "hallway"]


@dataclass
class HouseSpec:
    preset: str = "realistic"
    zigbee_devices: int = 28
    router_clients: int = 30
    junk_percent: int = 15  # share of devices that come out as junk of some kind
    core_extras: bool = True  # sun/weather/backup/update/... entities every HA install has
    seed: int = 42

    @classmethod
    def from_preset(cls, name: str, **override: Any) -> HouseSpec:
        if name not in PRESETS:
            raise ValueError(f"unknown preset {name!r}; choose one of {sorted(PRESETS)}")
        return cls(preset=name, **{**PRESETS[name], **override})

    def clamp(self) -> HouseSpec:
        self.zigbee_devices = max(0, min(int(self.zigbee_devices), 400))
        self.router_clients = max(0, min(int(self.router_clients), 250))
        self.junk_percent = max(0, min(int(self.junk_percent), 80))
        return self

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Stream:
    """How an entity changes when the house is alive. `kind` is read by sim/life.py."""

    kind: str  # walk | counter | pulse | flip | flap | battery | static
    every: float  # mean seconds between updates at rate 1.0
    lo: float = 0.0
    hi: float = 100.0
    step: float = 1.0
    digits: int = 1
    states: tuple[str, ...] = ()  # for flip/pulse: (resting, active)
    hold: float = 0.0  # pulse: seconds in the active state


@dataclass
class Built:
    entities: int = 0
    devices: int = 0
    registry_only: int = 0  # disabled entities: in the registry, no state (like real HA)
    junk: dict[str, int] = field(default_factory=dict)


# ---- Zigbee2MQTT catalogue ------------------------------------------------------------------
# (key, manufacturer, model, description, room kinds it fits)
ZIGBEE_MODELS = [
    ("motion", "Aqara", "RTCGQ11LM", "Aqara human body movement and illuminance sensor", "any"),
    ("climate", "Aqara", "WSDCGQ11LM", "Aqara temperature, humidity and pressure sensor", "any"),
    ("contact", "Aqara", "MCCGQ11LM", "Aqara door and window contact sensor", "any"),
    ("bulb", "IKEA", "LED2101G4", "TRADFRI bulb E27, white spectrum, globe, opal, 1055 lm", "any"),
    ("plug", "Tuya", "TS011F_plug_1", "Smart plug (with power monitoring)", "any"),
    ("button", "Aqara", "WXKG11LM", "Aqara wireless mini switch", "any"),
    ("leak", "Aqara", "SJCGQ11LM", "Aqara water leak sensor", "wet"),
    ("smoke", "Heiman", "HS1SA", "Smoke detector", "dry"),
    ("trv", "Tuya", "TS0601_thermostat", "Radiator valve with thermostat", "any"),
    ("blind", "IKEA", "E1757", "FYRTUR roller blind", "any"),
]
WEIGHTS = [5, 5, 5, 5, 4, 3, 1, 1, 2, 1]
NICE = {
    "motion": "Motion",
    "climate": "Climate",
    "contact": "Window",
    "bulb": "Ceiling Light",
    "plug": "Plug",
    "button": "Button",
    "leak": "Leak Sensor",
    "smoke": "Smoke Detector",
    "trv": "Radiator",
    "blind": "Blind",
}
PLUG_LOADS = [
    "Washing Machine",
    "Dishwasher",
    "Fridge",
    "TV Stand",
    "Desk",
    "Coffee Machine",
    "Kettle",
    "Router",
]
LINKQ = Stream("walk", 90, 20, 255, 12, 0)
BATTERY = Stream("battery", 3600, 0, 100, 1, 0)
VOLTAGE = Stream("walk", 900, 2800, 3100, 5, 0)
DEVTEMP = Stream("walk", 600, 18, 32, 1, 0)

# ---- router catalogue ------------------------------------------------------------------------
CLIENT_NAMES = [
    "iPhone",
    "iPhone-2",
    "Galaxy-S24",
    "android-3f2a9b1c4d5e6f70",
    "MacBook-Pro",
    "DESKTOP-7Q2K1LM",
    "LG-webOS-TV",
    "Chromecast",
    "ESP_A1B2C3",
    "ESP_4F3E21",
    "shellyplus1-a8032ab12c3d",
    "tasmota-5C1F",
    "Echo-Dot",
    "Nintendo-Switch",
    "PS5",
    "Kindle",
    "HP-LaserJet",
    "Roborock-S7",
    "Sonos-Bedroom",
    "raspberrypi",
    "jetson-orin",
    "homeassistant",
    "Watch",
    "iPad",
    "Galaxy-Tab-A8",
    "ring-doorbell",
    "Nest-Mini",
    "WLED-Kitchen",
    "Tuya-SmartLife",
    "unknown",
]
PEOPLE_DEVICES = {"iPhone", "Galaxy-S24", "Watch"}  # these follow the owner home / away


def _slug(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in text)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


class _Builder:
    def __init__(self, ha: HAState, spec: HouseSpec):
        self.ha, self.spec = ha, spec
        self.rng = random.Random(spec.seed)
        self.built = Built()
        self.streams: dict[str, Stream] = {}
        self.when = datetime.now(UTC) - timedelta(hours=2)

    # -- primitives
    def device(
        self,
        name: str,
        manufacturer: str,
        model: str,
        area: str | None,
        via: str | None = None,
        platform: str = "mqtt",
    ) -> str:
        dev_id = uuid.UUID(int=self.rng.getrandbits(128)).hex
        self.ha.devices.append(
            {
                "id": dev_id,
                "area_id": area,
                "name": name,
                "name_by_user": None,
                "manufacturer": manufacturer,
                "model": model,
                "disabled_by": None,
                "via_device_id": via,
                "sim_generated": True,
                "identifiers": [[platform, _slug(name)]],
            }
        )
        self.built.devices += 1
        return dev_id

    def entity(
        self,
        eid: str,
        state: str | None,
        attrs: dict,
        dev: str | None,
        *,
        area: str | None = None,
        platform: str = "mqtt",
        category: str | None = None,
        disabled: bool = False,
        hidden: bool = False,
        stream: Stream | None = None,
        name: str | None = None,
    ) -> None:
        if eid in self.ha.states or any(e["entity_id"] == eid for e in self.ha.entity_registry):
            return  # never shadow the base house or an earlier generated entity
        self.ha.entity_registry.append(
            {
                "entity_id": eid,
                "area_id": area,
                "device_id": dev,
                "platform": platform,
                "name": None,
                "original_name": name or attrs.get("friendly_name"),
                "disabled_by": "integration" if disabled else None,
                "hidden_by": "integration" if hidden else None,
                "entity_category": category,
                "unique_id": f"{platform}-{eid}",
                "labels": [],
                "id": uuid.UUID(int=self.rng.getrandbits(128)).hex,
                "sim_generated": True,
            }
        )
        self.ha.generated.add(eid)
        if disabled:
            self.built.registry_only += 1
            return
        self.ha.set_state(eid, state if state is not None else "unknown", attrs, ts=self.when)
        self.built.entities += 1
        if stream and state not in ("unavailable", "unknown"):
            self.streams[eid] = stream

    def junk(self, kind: str) -> None:
        self.built.junk[kind] = self.built.junk.get(kind, 0) + 1

    def roll(self) -> bool:
        return self.rng.random() * 100 < self.spec.junk_percent

    # -- Zigbee2MQTT
    def zigbee(self) -> None:
        bridge = self.device("Zigbee2MQTT Bridge", "Zigbee2MQTT", "Bridge", None)
        coord = self.device("Coordinator", "Texas Instruments", "CC2652P", None, via=bridge)
        for eid, state, attrs, cat in [
            (
                "binary_sensor.zigbee2mqtt_bridge_connection_state",
                "on",
                {"device_class": "connectivity"},
                None,
            ),
            ("sensor.zigbee2mqtt_bridge_version", "2.6.1", {"icon": "mdi:zigbee"}, "diagnostic"),
            ("sensor.zigbee2mqtt_bridge_coordinator_version", "20240710", {}, "diagnostic"),
            ("switch.zigbee2mqtt_bridge_permit_join", "off", {}, None),
            ("button.zigbee2mqtt_bridge_restart", "unknown", {"device_class": "restart"}, "config"),
            (
                "select.zigbee2mqtt_bridge_log_level",
                "info",
                {"options": ["error", "warning", "info", "debug"]},
                "config",
            ),
        ]:
            name = eid.split(".", 1)[1].replace("_", " ").title()
            self.entity(eid, state, {"friendly_name": name, **attrs}, bridge, category=cat)
        areas = BASE_AREAS + [a for a, _ in EXTRA_AREAS]
        used: dict[str, int] = {}
        by_key = {m[0]: m for m in ZIGBEE_MODELS}
        # a house with ten or more Zigbee devices has a leak and a smoke sensor, so those scenarios run
        must = [by_key["leak"], by_key["smoke"]] if self.spec.zigbee_devices >= 10 else []
        for i in range(self.spec.zigbee_devices):
            pick = must[i] if i < len(must) else self.rng.choices(ZIGBEE_MODELS, WEIGHTS)[0]
            key, man, model, desc, fits = pick
            area = self.rng.choice(
                [a for a in areas if fits == "any" or (fits == "wet") == (a in ("bathroom", "kitchen"))]
            )
            ieee = f"0x{self.rng.getrandbits(64):016x}"
            # at least one, if there is any junk: every real Zigbee network has one
            unrenamed = (self.roll() and self.rng.random() < 0.4) or (
                i == len(must) and self.spec.junk_percent > 0
            )
            if unrenamed:  # paired, never renamed, never given a room
                name, area = ieee, None
                self.junk("unrenamed_ieee")
            else:
                label = self.rng.choice(PLUG_LOADS) if key == "plug" else NICE[key]
                base = f"{area.replace('_', ' ').title()} {label}"
                used[base] = used.get(base, 0) + 1
                name = base if used[base] == 1 else f"{base} {used[base]}"
            dead = self.roll() and self.rng.random() < 0.3  # battery died / out of range
            dev = self.device(name, man, model, area, via=coord)
            self.ha.devices[-1]["model_id"] = model
            self.ha.devices[-1]["hw_version"] = desc
            self._zigbee_entities(key, _slug(name), name, dev, dead)
            if dead:
                self.junk("unavailable_device")
            if not unrenamed and self.roll() and self.rng.random() < 0.35:  # re-paired: old copy left behind
                suffix = self._primary_suffix(key)
                self.entity(
                    f"{self._primary(key)}.{_slug(name)}{'_' + suffix if suffix else ''}_2",
                    "unavailable",
                    {"friendly_name": f"{name} (old)", "restored": True, "supported_features": 0},
                    None,
                )
                self.junk("orphan_duplicate")

    @staticmethod
    def _primary(key: str) -> str:
        return {
            "motion": "binary_sensor",
            "climate": "sensor",
            "contact": "binary_sensor",
            "bulb": "light",
            "plug": "switch",
            "button": "event",
            "leak": "binary_sensor",
            "smoke": "binary_sensor",
            "trv": "climate",
            "blind": "cover",
        }[key]

    @staticmethod
    def _primary_suffix(key: str) -> str:
        return {
            "motion": "occupancy",
            "climate": "temperature",
            "contact": "contact",
            "bulb": "",
            "plug": "",
            "button": "action",
            "leak": "water_leak",
            "smoke": "smoke",
            "trv": "",
            "blind": "",
        }[key]

    def _zigbee_entities(self, key: str, obj: str, name: str, dev: str, dead: bool) -> None:
        r = self.rng
        st = (lambda v: "unavailable") if dead else (lambda v: v)
        battery = key in ("motion", "climate", "contact", "button", "leak", "smoke", "trv", "blind")

        def e(
            domain: str,
            suffix: str,
            state: Any,
            attrs: dict,
            cat: str | None = None,
            stream=None,
            disabled: bool = False,
        ) -> None:
            eid = f"{domain}.{obj}_{suffix}" if suffix else f"{domain}.{obj}"
            label = f"{name} {suffix.replace('_', ' ').title()}".strip() if suffix else name
            self.entity(
                eid,
                st(str(state)),
                {"friendly_name": label, **attrs},
                dev,
                category=cat,
                stream=stream,
                disabled=disabled,
            )

        if key == "motion":
            e(
                "binary_sensor",
                "occupancy",
                "off",
                {"device_class": "occupancy"},
                stream=Stream("pulse", 600, states=("off", "on"), hold=90),
            )
            e(
                "sensor",
                "illuminance",
                r.randint(0, 400),
                {"device_class": "illuminance", "unit_of_measurement": "lx", "state_class": "measurement"},
                stream=Stream("walk", 120, 0, 1200, 25, 0),
            )
            e("sensor", "power_outage_count", r.randint(0, 40), {}, "diagnostic", disabled=True)
        elif key == "climate":
            t = round(r.uniform(18.5, 23.5), 2)
            e(
                "sensor",
                "temperature",
                t,
                {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"},
                stream=Stream("walk", 240, 15, 28, 0.13, 2),
            )
            e(
                "sensor",
                "humidity",
                round(r.uniform(35, 65), 2),
                {"device_class": "humidity", "unit_of_measurement": "%", "state_class": "measurement"},
                stream=Stream("walk", 300, 25, 90, 0.6, 2),
            )
            e(
                "sensor",
                "pressure",
                round(r.uniform(990, 1030), 1),
                {
                    "device_class": "atmospheric_pressure",
                    "unit_of_measurement": "hPa",
                    "state_class": "measurement",
                },
                stream=Stream("walk", 900, 970, 1045, 0.3, 1),
            )
        elif key == "contact":
            e(
                "binary_sensor",
                "contact",
                "off",
                {"device_class": "window"},
                stream=Stream("flip", 5400, states=("off", "on")),
            )
            e(
                "sensor",
                "device_temperature",
                r.randint(19, 27),
                {"device_class": "temperature", "unit_of_measurement": "°C"},
                "diagnostic",
                DEVTEMP,
            )
        elif key == "bulb":
            on = r.random() < 0.3
            e(
                "light",
                "",
                "on" if on else "off",
                {
                    "supported_color_modes": ["color_temp"],
                    "color_mode": "color_temp" if on else None,
                    "brightness": r.randint(60, 254) if on else None,
                    "min_color_temp_kelvin": 2202,
                    "max_color_temp_kelvin": 4000,
                    "supported_features": 44,
                },
            )
            e(
                "select",
                "power_on_behavior",
                "previous",
                {"options": ["off", "on", "toggle", "previous"]},
                "config",
            )
            e(
                "select",
                "effect",
                "unknown",
                {"options": ["blink", "breathe", "okay", "channel_change", "finish_effect", "stop_effect"]},
                "config",
            )
        elif key == "plug":
            on = r.random() < 0.7
            watts = round(r.uniform(2, 120), 1) if on else 0
            e("switch", "", "on" if on else "off", {"device_class": "outlet"})
            e(
                "sensor",
                "power",
                watts,
                {"device_class": "power", "unit_of_measurement": "W", "state_class": "measurement"},
                stream=Stream("walk", 8, 0, 2400, 6, 1) if on else None,
            )
            e(
                "sensor",
                "energy",
                round(r.uniform(1, 900), 2),
                {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing"},
                stream=Stream("counter", 60, 0, 1e6, 0.004, 2),
            )
            e(
                "sensor",
                "voltage",
                round(r.uniform(226, 236), 1),
                {"device_class": "voltage", "unit_of_measurement": "V", "state_class": "measurement"},
                stream=Stream("walk", 30, 215, 245, 0.6, 1),
            )
            e(
                "sensor",
                "current",
                round(watts / 230, 2),
                {"device_class": "current", "unit_of_measurement": "A", "state_class": "measurement"},
                stream=Stream("walk", 15, 0, 10, 0.03, 2),
            )
            e("select", "power_outage_memory", "restore", {"options": ["on", "off", "restore"]}, "config")
            e("select", "indicator_mode", "off/on", {"options": ["off", "off/on", "on/off", "on"]}, "config")
            e("lock", "child_lock", "unlocked", {}, "config")
        elif key == "button":
            e(
                "event",
                "action",
                "unknown",
                {
                    "event_types": ["single", "double", "triple", "quadruple", "hold", "release"],
                    "event_type": None,
                    "device_class": "button",
                },
                stream=Stream("flip", 7200, states=("single", "double")),
            )
        elif key == "leak":
            e("binary_sensor", "water_leak", "off", {"device_class": "moisture"})
            e("binary_sensor", "battery_low", "off", {"device_class": "battery"}, "diagnostic")
        elif key == "smoke":
            e("binary_sensor", "smoke", "off", {"device_class": "smoke"})
            e("binary_sensor", "tamper", "off", {"device_class": "tamper"}, "diagnostic")
            e("binary_sensor", "battery_low", "off", {"device_class": "battery"}, "diagnostic")
        elif key == "trv":
            cur = round(r.uniform(18, 22), 1)
            e(
                "climate",
                "",
                "heat",
                {
                    "hvac_modes": ["off", "heat", "auto"],
                    "current_temperature": cur,
                    "temperature": 21,
                    "min_temp": 5,
                    "max_temp": 35,
                    "target_temp_step": 0.5,
                    "hvac_action": "idle",
                    "preset_modes": ["manual", "schedule", "holiday", "boost", "eco"],
                    "preset_mode": "manual",
                },
            )
            e("switch", "window_detection", "on", {}, "config")
            e("lock", "child_lock", "unlocked", {}, "config")
            e(
                "sensor",
                "position",
                r.randint(0, 100),
                {"unit_of_measurement": "%"},
                "diagnostic",
                Stream("walk", 600, 0, 100, 8, 0),
            )
        elif key == "blind":
            pos = r.choice([0, 100, 100, 40])
            e(
                "cover",
                "",
                "open" if pos else "closed",
                {"device_class": "blind", "current_position": pos, "supported_features": 15},
            )
        if battery:
            e(
                "sensor",
                "battery",
                r.randint(3, 100),
                {"device_class": "battery", "unit_of_measurement": "%", "state_class": "measurement"},
                "diagnostic",
                BATTERY,
            )
            e(
                "sensor",
                "voltage",
                r.randint(2850, 3100),
                {"device_class": "voltage", "unit_of_measurement": "mV", "state_class": "measurement"},
                "diagnostic",
                VOLTAGE,
            )
        e(
            "sensor",
            "linkquality",
            r.randint(30, 255),
            {"unit_of_measurement": "lqi", "state_class": "measurement", "icon": "mdi:signal"},
            "diagnostic",
            LINKQ,
            disabled=r.random() < 0.3,
        )
        e(
            "sensor",
            "last_seen",
            "unknown" if r.random() < 0.5 else self.when.isoformat(),
            {"device_class": "timestamp"},
            "diagnostic",
            disabled=r.random() < 0.6,
        )
        e(
            "update",
            "",
            "off" if r.random() < 0.8 else "on",
            {
                "device_class": "firmware",
                "installed_version": "0x00000017",
                "latest_version": "0x00000019",
                "auto_update": False,
                "title": None,
                "in_progress": False,
                "supported_features": 5,
            },
            "config",
        )

    # -- router
    def router(self) -> None:
        if self.spec.router_clients <= 0:
            return
        box = self.device("Router", "AVM", "FRITZ!Box 7590 AX", "hallway", platform="fritz")
        p = "router"
        for eid, state, attrs, cat, stream in [
            (f"binary_sensor.{p}_connection", "on", {"device_class": "connectivity"}, "diagnostic", None),
            (f"binary_sensor.{p}_link", "on", {"device_class": "plug"}, "diagnostic", None),
            (
                f"sensor.{p}_external_ip",
                f"203.0.113.{self.rng.randint(2, 250)}",
                {"icon": "mdi:earth"},
                None,
                None,
            ),
            (f"sensor.{p}_external_ipv6", "2001:db8::1", {"icon": "mdi:earth"}, None, None),
            (
                f"sensor.{p}_connection_uptime",
                (self.when - timedelta(days=11)).isoformat(),
                {"device_class": "timestamp"},
                "diagnostic",
                None,
            ),
            (
                f"sensor.{p}_last_restart",
                (self.when - timedelta(days=11)).isoformat(),
                {"device_class": "timestamp"},
                "diagnostic",
                None,
            ),
            (
                f"sensor.{p}_download_throughput",
                "812.4",
                {"unit_of_measurement": "kB/s", "state_class": "measurement", "device_class": "data_rate"},
                None,
                Stream("walk", 10, 0, 60000, 900, 1),
            ),
            (
                f"sensor.{p}_upload_throughput",
                "96.1",
                {"unit_of_measurement": "kB/s", "state_class": "measurement", "device_class": "data_rate"},
                None,
                Stream("walk", 10, 0, 6000, 90, 1),
            ),
            (
                f"sensor.{p}_gb_received",
                "4123.8",
                {"unit_of_measurement": "GB", "state_class": "total_increasing", "device_class": "data_size"},
                None,
                Stream("counter", 120, 0, 1e7, 0.05, 1),
            ),
            (
                f"sensor.{p}_gb_sent",
                "512.3",
                {"unit_of_measurement": "GB", "state_class": "total_increasing", "device_class": "data_size"},
                None,
                Stream("counter", 120, 0, 1e7, 0.01, 1),
            ),
            (
                f"sensor.{p}_max_connection_download_throughput",
                "250.0",
                {"unit_of_measurement": "Mbit/s"},
                "diagnostic",
                None,
            ),
            (
                f"sensor.{p}_link_download_noise_margin",
                "9.0",
                {"unit_of_measurement": "dB"},
                "diagnostic",
                Stream("walk", 600, 4, 14, 0.5, 1),
            ),
            (f"switch.{p}_wi_fi_guest", "off", {"icon": "mdi:wifi"}, "config", None),
            (f"switch.{p}_call_deflection_0", "off", {}, "config", None),
            (f"button.{p}_reboot", "unknown", {"device_class": "restart"}, "config", None),
            (f"button.{p}_cleanup", "unknown", {}, "config", None),
            (
                f"update.{p}_fritz_os",
                "off",
                {
                    "device_class": "firmware",
                    "installed_version": "8.03",
                    "latest_version": "8.03",
                    "title": "FRITZ!OS",
                },
                "config",
                None,
            ),
            (f"image.{p}_guest_wi_fi", "unknown", {"access_token": "sim"}, "diagnostic", None),
        ]:
            name = "Router " + eid.split(".", 1)[1].removeprefix(f"{p}_").replace("_", " ").title()
            self.entity(
                eid,
                state,
                {"friendly_name": name, **attrs},
                box,
                platform="fritz",
                category=cat,
                stream=stream,
            )
        names = list(CLIENT_NAMES)
        for i in range(self.spec.router_clients):
            host = names[i] if i < len(names) else f"{self.rng.choice(names[:12])}-{i}"
            if host == "unknown" or (self.roll() and self.rng.random() < 0.3):
                mac = ":".join(f"{self.rng.randint(0, 255):02X}" for _ in range(6))
                host = f"Unknown {mac}"
                self.junk("unknown_client")
            obj = _slug(host)
            mac = ":".join(f"{self.rng.randint(0, 255):02X}" for _ in range(6))
            away = self.rng.random() < 0.25 and host not in PEOPLE_DEVICES
            dev = self.device(host, "AVM", "FRITZ!Box Tracked device", None, via=box, platform="fritz")
            stream = None  # the owner's phone and watch follow person.owner (sim/life.py, scenarios)
            if host not in PEOPLE_DEVICES and self.rng.random() < 0.3:
                stream = Stream("flip", 3600 * 3, states=("home", "not_home"))
            self.entity(
                f"device_tracker.{obj}",
                "not_home" if away else "home",
                {
                    "source_type": "router",
                    "ip": f"192.168.178.{20 + i}",
                    "mac": mac,
                    "host_name": host,
                    "last_time_reachable": self.when.isoformat(),
                    "friendly_name": host,
                },
                dev,
                platform="fritz",
                stream=stream,
            )
            self.entity(
                f"switch.{obj}_internet_access",
                "on",
                {"friendly_name": f"{host} Internet Access", "icon": "mdi:check-network"},
                dev,
                platform="fritz",
                category="config",
                disabled=self.rng.random() < 0.5,
            )

    # -- Home Assistant's own housekeeping
    def core(self) -> None:
        if not self.spec.core_extras:
            return
        now = self.when
        for eid, state, attrs, cat, platform in [
            (
                "sensor.sun_next_dawn",
                (now + timedelta(hours=14)).isoformat(),
                {"device_class": "timestamp"},
                "diagnostic",
                "sun",
            ),
            (
                "sensor.sun_next_dusk",
                (now + timedelta(hours=5)).isoformat(),
                {"device_class": "timestamp"},
                "diagnostic",
                "sun",
            ),
            (
                "sensor.sun_next_rising",
                (now + timedelta(hours=15)).isoformat(),
                {"device_class": "timestamp"},
                "diagnostic",
                "sun",
            ),
            (
                "sensor.sun_next_setting",
                (now + timedelta(hours=4)).isoformat(),
                {"device_class": "timestamp"},
                "diagnostic",
                "sun",
            ),
            (
                "weather.forecast_home",
                "cloudy",
                {
                    "temperature": 7.4,
                    "humidity": 81,
                    "pressure": 1012.3,
                    "wind_speed": 14.8,
                    "temperature_unit": "°C",
                    "supported_features": 3,
                },
                None,
                "met",
            ),
            (
                "update.home_assistant_core_update",
                "off",
                {
                    "installed_version": "2026.11.1",
                    "latest_version": "2026.11.1",
                    "title": "Home Assistant Core",
                },
                None,
                "hassio",
            ),
            (
                "update.home_assistant_operating_system_update",
                "on",
                {
                    "installed_version": "16.2",
                    "latest_version": "16.3",
                    "title": "Home Assistant Operating System",
                },
                None,
                "hassio",
            ),
            (
                "update.home_assistant_supervisor_update",
                "off",
                {"installed_version": "2026.11.0", "latest_version": "2026.11.0"},
                None,
                "hassio",
            ),
            (
                "update.agent_house_update",
                "off",
                {"installed_version": "0.1.0", "latest_version": "0.1.0", "title": "Agent House"},
                None,
                "hassio",
            ),
            (
                "update.mosquitto_broker_update",
                "off",
                {"installed_version": "6.5.1", "latest_version": "6.5.1"},
                None,
                "hassio",
            ),
            (
                "update.zigbee2mqtt_update",
                "on",
                {"installed_version": "2.6.0-1", "latest_version": "2.6.1-1"},
                None,
                "hassio",
            ),
            ("sensor.backup_backup_manager_state", "idle", {"device_class": "enum"}, None, "backup"),
            (
                "sensor.backup_next_scheduled_automatic_backup",
                (now + timedelta(hours=9)).isoformat(),
                {"device_class": "timestamp"},
                None,
                "backup",
            ),
            (
                "sensor.backup_last_successful_automatic_backup",
                (now - timedelta(hours=15)).isoformat(),
                {"device_class": "timestamp"},
                None,
                "backup",
            ),
            (
                "zone.home",
                "1",
                {
                    "latitude": 52.5,
                    "longitude": 13.4,
                    "radius": 100,
                    "passive": False,
                    "persons": ["person.owner"],
                },
                None,
                "zone",
            ),
            ("conversation.home_assistant", "unknown", {"supported_features": 1}, None, "conversation"),
            ("tts.google_translate_en_com", "unknown", {}, None, "google_translate"),
            ("todo.shopping_list", "3", {"supported_features": 15}, None, "shopping_list"),
            (
                "input_boolean.guest_mode",
                "off",
                {"editable": True, "icon": "mdi:account-multiple"},
                None,
                "input_boolean",
            ),
            ("input_boolean.vacation", "off", {"editable": True}, None, "input_boolean"),
            (
                "input_number.target_temperature",
                "21.0",
                {"min": 15, "max": 25, "step": 0.5, "mode": "slider", "unit_of_measurement": "°C"},
                None,
                "input_number",
            ),
            (
                "sensor.processor_use",
                "7",
                {"unit_of_measurement": "%", "state_class": "measurement"},
                None,
                "systemmonitor",
            ),
            (
                "sensor.memory_use_percent",
                "41.2",
                {"unit_of_measurement": "%", "state_class": "measurement"},
                None,
                "systemmonitor",
            ),
            (
                "sensor.disk_use_percent",
                "23.9",
                {"unit_of_measurement": "%", "state_class": "measurement"},
                None,
                "systemmonitor",
            ),
        ]:
            name = eid.split(".", 1)[1].replace("_", " ").title()
            stream = None
            if eid.startswith("sensor.processor"):
                stream = Stream("walk", 15, 1, 100, 3, 0)
            elif eid.startswith("sensor.memory"):
                stream = Stream("walk", 60, 20, 95, 0.4, 1)
            self.entity(
                eid,
                state,
                {"friendly_name": name, **attrs},
                None,
                platform=platform,
                category=cat,
                stream=stream,
            )
        # leftovers of integrations that were removed long ago: orphaned, restored, unavailable
        for eid in [
            "sensor.old_hue_bridge_temperature",
            "light.living_room_strip_old",
            "binary_sensor.ring_front_door_ding",
            "sensor.sm_g991b_battery_level",
            "switch.tasmota_test",
            "media_player.chromecast_kitchen_2",
        ]:
            if self.spec.junk_percent and self.rng.random() < 0.3 + self.spec.junk_percent / 100:
                self.entity(
                    eid,
                    "unavailable",
                    {
                        "friendly_name": eid.split(".", 1)[1].replace("_", " ").title(),
                        "restored": True,
                        "supported_features": 0,
                    },
                    None,
                    platform="unknown",
                )
                self.junk("orphan_restored")


def build(ha: HAState, spec: HouseSpec) -> tuple[Built, dict[str, Stream]]:
    """Remove the previous generated house, then grow a new one from `spec`."""
    spec.clamp()
    clear(ha)
    b = _Builder(ha, spec)
    # Like an integration reload in real HA: a state_changed per entity, registry events at the end.
    # The sim's own listeners (event log, automation runner) sit this out.
    listeners, ha.listeners = ha.listeners, []
    try:
        if spec.zigbee_devices or spec.router_clients or spec.core_extras:
            known = {a["area_id"] for a in ha.areas}
            for area_id, name in EXTRA_AREAS:
                if area_id not in known:
                    ha.areas.append(
                        {
                            "area_id": area_id,
                            "name": name,
                            "floor_id": None,
                            "aliases": [],
                            "icon": None,
                            "labels": [],
                            "picture": None,
                            "sim_generated": True,
                        }
                    )
        if spec.zigbee_devices:
            b.zigbee()
        b.router()
        b.core()
    finally:
        ha.listeners = listeners
    announce(ha)
    return b.built, b.streams


def clear(ha: HAState) -> None:
    gen = ha.generated
    for eid in list(gen):  # state_changed with new_state None, as real HA sends for removed entities
        ha.remove_state(eid)
        ha.history.pop(eid, None)
    ha.entity_registry = [e for e in ha.entity_registry if not e.get("sim_generated")]
    ha.devices = [d for d in ha.devices if not d.get("sim_generated")]
    ha.areas = [a for a in ha.areas if not a.get("sim_generated")]
    ha.logbook_rows = [r for r in ha.logbook_rows if r.get("entity_id") not in gen]
    gen.clear()


def announce(ha: HAState) -> None:
    """What real HA fires after an integration (re)loads: the app re-reads registries and states."""
    for event in ("area_registry_updated", "device_registry_updated", "entity_registry_updated"):
        ha.fire_event(event, {"action": "update"})


def summary(ha: HAState) -> dict:
    states = ha.states.values()
    dev_area = {d["id"]: d.get("area_id") for d in ha.devices}
    by_domain: dict[str, int] = {}
    for s in states:
        d = s["entity_id"].split(".")[0]
        by_domain[d] = by_domain.get(d, 0) + 1
    return {
        "entities": len(ha.states),
        "registry": len(ha.entity_registry),
        "devices": len(ha.devices),
        "areas": len(ha.areas),
        "generated": len(ha.generated),
        "unavailable": sum(1 for s in states if s["state"] == "unavailable"),
        "unknown": sum(1 for s in states if s["state"] == "unknown"),
        "disabled": sum(1 for e in ha.entity_registry if e.get("disabled_by")),
        "no_area": sum(
            1 for e in ha.entity_registry if not e.get("area_id") and not dev_area.get(e.get("device_id"))
        ),
        "by_domain": dict(sorted(by_domain.items(), key=lambda kv: -kv[1])),
    }
