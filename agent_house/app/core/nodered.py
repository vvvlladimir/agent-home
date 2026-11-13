"""Read a Node-RED flow export (the JSON array of nodes) into "originals": one per top-level group
of a tab, plus one for the tab's ungrouped nodes. Read-only and deterministic; no model involved.

Only what can be read without running the flow is kept, in Home Assistant's own shape:
- triggers:   `server-state-changed` → state, `ha-time` → time, `inject`/`cronplus` → time_pattern,
              `mqtt in` → mqtt, `server-events` → event;
- conditions: `api-current-state` (entity + expected state);
- actions:    `api-call-service` (literal `domain.service`, entities, raw data).
Subflow instances are expanded with their `env` values (`${BULB_ENTITY}` → the instance's entity).
`function`, `switch` and `change` nodes are the flow's logic; they are only counted, so the summary
says how much of the flow could not be read.
"""

from __future__ import annotations

import json
import re

ENV = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
LOGIC_TYPES = ("function", "switch", "change", "trigger", "delay")
MAX_DEPTH = 3  # subflows inside subflows


def _list(value) -> list:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, list) else [value]


def _env_of(node: dict) -> dict[str, str]:
    return {
        str(e.get("name")): str(e.get("value", ""))
        for e in node.get("env") or []
        if isinstance(e, dict) and e.get("name")
    }


def _subst(value, env: dict[str, str]):
    if isinstance(value, str):
        return ENV.sub(lambda m: env.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [_subst(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _subst(v, env) for k, v in value.items()}
    return value


def _entities(values) -> list[str]:
    return [v for v in _list(values) if isinstance(v, str) and ENTITY.fullmatch(v)]


class _Flow:
    def __init__(self, nodes: list[dict]) -> None:
        self.nodes = [n for n in nodes if isinstance(n, dict) and n.get("id")]
        self.by_id = {n["id"]: n for n in self.nodes}
        self.children: dict[str, list[dict]] = {}
        for node in self.nodes:
            if node.get("z"):
                self.children.setdefault(node["z"], []).append(node)

    def expand(self, nodes: list[dict], env: dict[str, str], depth: int = 0) -> list[dict]:
        """The nodes with `${VAR}` replaced, and subflow instances replaced by their contents."""
        out = []
        for node in nodes:
            kind = str(node.get("type") or "")
            if kind.startswith("subflow:") and depth < MAX_DEPTH:
                template = self.by_id.get(kind.split(":", 1)[1]) or {}
                inner = {**_env_of(template), **{k: _subst(v, env) for k, v in _env_of(node).items()}}
                inside = [n for n in self.children.get(template.get("id", ""), []) if not n.get("d")]
                out += self.expand(inside, inner, depth + 1)
            else:
                out.append(_subst(node, env) if env else node)
        return out

    def group_nodes(self, group: dict) -> list[dict]:
        out = []
        for node_id in group.get("nodes") or []:
            node = self.by_id.get(node_id)
            if node is None:
                continue
            out += self.group_nodes(node) if node.get("type") == "group" else [node]
        return out


def _read(nodes: list[dict]) -> dict:
    triggers, conditions, actions, unread = [], [], [], []
    logic: dict[str, int] = {}
    for node in nodes:
        kind = node.get("type")
        if kind == "server-state-changed":
            spec = node.get("entities") if isinstance(node.get("entities"), dict) else {}
            entities = _entities(spec.get("entity")) or _entities(node.get("entityId"))
            if not entities:
                unread.append(f"state trigger {node.get('name') or node['id']!r} without a literal entity")
                continue
            trigger: dict = {"trigger": "state", "entity_id": entities}
            if node.get("ifState") not in (None, "") and node.get("ifStateOperator") in ("is", None):
                trigger["to"] = str(node["ifState"])
            triggers.append(trigger)
        elif kind == "ha-time":
            if ENTITY.fullmatch(str(node.get("entityId") or "")):
                trigger = {"trigger": "time", "at": node["entityId"]}
                if node.get("offset") not in (None, "", "0", 0):
                    trigger["offset"] = f"{node['offset']} {node.get('offsetUnits') or 'minutes'}"
                triggers.append(trigger)
        elif kind == "inject":
            if node.get("repeat"):
                triggers.append({"trigger": "time_pattern", "every_s": str(node["repeat"])})
            elif node.get("crontab"):
                triggers.append({"trigger": "time_pattern", "cron": str(node["crontab"])})
        elif kind == "cronplus":
            for option in node.get("options") or []:
                if isinstance(option, dict) and option.get("expression"):
                    triggers.append({"trigger": "time_pattern", "cron": str(option["expression"])})
        elif kind == "mqtt in":
            triggers.append({"trigger": "mqtt", "topic": str(node.get("topic") or "")})
        elif kind == "server-events":
            triggers.append({"trigger": "event", "event_type": str(node.get("eventType") or "")})
        elif kind == "api-current-state":
            entity = str(node.get("entity_id") or "")
            if ENTITY.fullmatch(entity):
                condition: dict = {"condition": "state", "entity_id": entity}
                if node.get("halt_if") not in (None, ""):
                    condition["state"] = str(node["halt_if"])
                conditions.append(condition)
        elif kind == "api-call-service":
            service = node.get("action") or f"{node.get('domain') or ''}.{node.get('service') or ''}"
            entities = _entities(node.get("entityId"))
            if not ENTITY.fullmatch(str(service)):
                unread.append(f"service call {node.get('name') or node['id']!r} with a computed service")
                continue
            action: dict = {"action": service}
            if entities:
                action["target"] = {"entity_id": entities}
            else:
                unread.append(f"{service} with computed entities")
            if node.get("data") not in (None, "", "{}"):
                action["data_expression"] = str(node["data"])[:200]
            actions.append(action)
        elif kind in LOGIC_TYPES:
            logic[kind] = logic.get(kind, 0) + 1

    def unique(items: list[dict]) -> list[dict]:
        seen, out = set(), []
        for item in items:
            key = json.dumps(item, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    return {
        "triggers": unique(triggers),
        "conditions": unique(conditions),
        "actions": unique(actions),
        "logic_nodes": logic,
        "unread": sorted(set(unread)),
        "nodes": len(nodes),
    }


def parse_flows(text: str, file_stem: str) -> list[dict]:
    """→ [{key, title, enabled, config}]; `key` is stable across re-imports. Raises ValueError."""
    try:
        nodes = json.loads(text)
    except ValueError as err:
        raise ValueError(f"not JSON: {err}") from None
    if not isinstance(nodes, list):
        raise ValueError("a Node-RED export is a JSON array of nodes")
    flow = _Flow(nodes)
    out = []
    for tab in [n for n in flow.nodes if n.get("type") == "tab"]:
        on_tab = flow.children.get(tab["id"], [])
        tab_env = _env_of(tab)
        units = []
        for group in on_tab:
            if group.get("type") != "group" or group.get("g"):
                continue
            members = flow.group_nodes(group)
            # owners label groups with a comment node more often than with the group's own name
            named = [n for n in members if n.get("name") and n.get("type") == "comment"] or [
                n for n in members if n.get("name")
            ]
            name = group.get("name") or (named[0]["name"] if named else "group")
            units.append((group["id"], str(name), members))
        loose = [n for n in on_tab if n.get("type") != "group" and not n.get("g")]
        if loose:
            units.append(("ungrouped", "ungrouped nodes", loose))
        for key, name, members in units:
            active = [n for n in members if not n.get("d")]
            config = _read(flow.expand(active, tab_env))
            if not config["triggers"] and not config["actions"]:
                continue  # comments, config nodes, pure helpers
            label = str(tab.get("label") or file_stem)
            out.append(
                {
                    "key": f"{file_stem}_{key}",
                    "title": f"{label}: {name}"[:120],
                    "enabled": False if tab.get("disabled") or not active else None,
                    "config": {"flow": label, **config},
                }
            )
    return out
