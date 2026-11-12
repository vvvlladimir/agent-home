"""Area → entity mapping built from HA's area, device and entity registries.

An entity belongs to the area set on its registry entry, else to the area of its device.
Entities that exist only as states (no registry entry, e.g. YAML-defined) have no area.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Registry:
    areas: dict[str, dict] = field(default_factory=dict)  # area_id -> {area_id, name, ...}
    entity_area: dict[str, str | None] = field(default_factory=dict)
    entity_meta: dict[str, dict] = field(default_factory=dict)  # registry entry per entity_id

    @classmethod
    def build(cls, areas: list[dict], devices: list[dict], entities: list[dict]) -> Registry:
        device_area = {d.get("id"): d.get("area_id") for d in devices if d.get("id")}
        reg = cls(areas={a["area_id"]: a for a in areas if a.get("area_id")})
        for entry in entities:
            entity_id = entry.get("entity_id")
            if not entity_id:
                continue
            area = entry.get("area_id") or device_area.get(entry.get("device_id"))
            reg.entity_area[entity_id] = area if area in reg.areas else None
            reg.entity_meta[entity_id] = entry
        return reg

    def area_of(self, entity_id: str) -> str | None:
        return self.entity_area.get(entity_id)

    def entities_in(self, area_id: str) -> list[str]:
        return sorted(e for e, a in self.entity_area.items() if a == area_id)

    def is_hidden(self, entity_id: str) -> bool:
        meta = self.entity_meta.get(entity_id, {})
        return bool(meta.get("disabled_by") or meta.get("hidden_by"))

    def name_of(self, entity_id: str) -> str | None:
        meta = self.entity_meta.get(entity_id, {})
        return meta.get("name") or meta.get("original_name")
