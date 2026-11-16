"""The link to the Jetson learner (D38): the house goes there, what it learnt comes back.

- Forward: every state change, with its cause read from the HA context the way core/insights.py reads
  the logbook (an automation's or script's run context → automation/script, our own user → agent, a
  program user such as Node-RED → automation, another user → user, a parent context → other, nothing
  → physical); automation runs; answers to our ask notifications. Numeric sensors at most once a
  minute (the last value). A bounded buffer while the Jetson is away; what fell out of it, and the
  history before the first contact, is filled from HA's logbook (day by day, `BACKFILL_DAYS` at most).
- The registry (areas, entities, our automations, the tier policy) on start and when it changes.
- Back: the house model every `MODEL_S` (kv `learner_model`); its longer-history tables replace the
  add-on's own for the event gate and the unusual-state notes while it is fresh; a light controller's
  timeout that the owner's corrections ask to change becomes a finding.
- Live suggestions: an `act` the learner has earned goes through the actuator as agent `learner`, like
  any agent action: policy, tiers, ownership (the learner may act only where the orchestrator owns the
  entity, i.e. no room agent does), the kill switch, and shadow unless `learner_mode: live` (and the
  global mode is live). Once per device and state per stretch, at most once an hour.
The learner holds no HA token; nothing here writes to HA except through the actuator.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict, deque
from datetime import UTC, datetime
from itertools import islice

import httpx

from . import clock, predict
from .habits import ACT_DOMAINS
from .insights import changes_from_logbook, fires_from_logbook

log = logging.getLogger(__name__)

LEARNER = "learner"
PUSH_S = 2.0
MODEL_S = 600.0
REGISTRY_S = 6 * 3600.0
STATUS_S = 60.0
BATCH = 2000
BUFFER = 50_000
BACKFILL_DAYS = 60
CONTEXTS = 5000  # automation/script run contexts remembered (an automation with a delay acts later)
FRESH_S = 48 * 3600.0  # a house model older than this is not used
NUMERIC_DOMAINS = frozenset({"sensor", "number", "input_number"})
ASK_PREFIX = "AGENT_HOUSE_"
ANSWER_ENTITY = "event.agent_house_ask"
MAX_STATE = 255
TIMEOUT_MIN_CORRECTIONS = 0.3  # a day
FACTS = 12
# devices the learner may act on live; helpers (input_boolean / input_select, the house mode) never
LIVE_DOMAINS = frozenset(ACT_DOMAINS - {"input_boolean", "input_select"})


def _ts(value) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()
    return None


def _numeric(entity_id: str, state: str) -> bool:
    if entity_id.split(".", 1)[0] not in NUMERIC_DOMAINS:
        return False
    try:
        float(state)
    except ValueError:
        return False
    return True


class LearnerLink:
    def __init__(self, settings, runtime, store, feed, ha, insights, safety) -> None:
        self.settings, self.runtime, self.store, self.feed = settings, runtime, store, feed
        self.ha, self.insights, self.safety = ha, insights, safety
        self.url = (settings.jetson_learner_url or "").rstrip("/")
        self.actuator = None  # wired by main
        self.buffer: deque[dict] = deque(maxlen=BUFFER)
        self.dropped = self.pushed = 0
        self.last_push: float | None = None
        self.connected = False
        self.error: str | None = None
        self.backfill: dict = {"state": "idle"}
        self.registry_sent: float | None = None
        self.jetson: dict | None = None
        self._numeric: dict[str, tuple[float, str]] = {}
        self._contexts: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._programs: dict[str, str] = {}
        self._programs_at = 0.0
        self._live_since: float | None = None
        self._need_backfill = True
        self._registry_sig: str | None = None
        self._model_at = self._status_at = 0.0
        self._acted: dict[tuple[str, str], float] = {}
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=3.0),
            headers={"Authorization": f"Bearer {settings.jetson_token}"},
        )
        self._task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def mode(self) -> str:
        return "live" if getattr(self.runtime, "learner_mode", "shadow") == "live" else "shadow"

    # --- lifecycle --------------------------------------------------------------------------------
    async def start(self) -> None:
        if self.configured:
            self._task = asyncio.create_task(self._loop(), name="learner-link")

    async def stop(self) -> None:
        for task in (self._task, self._backfill_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self._task, self._backfill_task) if t), return_exceptions=True)
        await self._http.aclose()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(PUSH_S)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - one bad tick must not stop the link
                log.exception("learner link tick failed")

    # --- what happens in the house ----------------------------------------------------------------
    def cause(self, new: dict) -> tuple[str, str | None]:
        context = new.get("context") if isinstance(new.get("context"), dict) else {}
        hit = self._contexts.get(context.get("id") or "")
        if hit:
            return hit
        user, parent = context.get("user_id"), context.get("parent_id")
        own = getattr(self.ha, "own_user_id", None)
        if user and own and user == own:
            return "agent", None
        if user and user in self._programs:
            return "automation", f"user:{self._programs[user]}"
        if user:
            return "user", None
        if parent:
            return "other", None
        return "physical", None

    def _add(self, event: dict) -> None:
        if len(self.buffer) == self.buffer.maxlen:
            self.dropped += 1
            self._need_backfill = True
        self.buffer.append(event)
        if self._live_since is None:
            self._live_since = event["ts"]

    def on_state(self, entity_id: str, old: dict | None, new: dict | None) -> None:
        if not self.configured or new is None:
            return
        state = str(new.get("state"))[:MAX_STATE]
        if old is not None and old.get("state") == new.get("state"):
            return  # attribute-only
        ts = _ts(new.get("last_changed")) or clock.time()
        if _numeric(entity_id, state):
            self._numeric[entity_id] = (ts, state)
            return
        cause, ref = self.cause(new)
        self._add(
            {"ts": ts, "kind": "state", "entity_id": entity_id, "state": state, "cause": cause, "ref": ref}
        )

    def on_event(self, event: dict) -> None:
        if not self.configured:
            return
        kind, data = event.get("event_type"), event.get("data") or {}
        ts = _ts(event.get("time_fired")) or clock.time()
        context = event.get("context") if isinstance(event.get("context"), dict) else {}
        if kind in ("automation_triggered", "script_started"):
            entity_id = data.get("entity_id")
            if not isinstance(entity_id, str) or "." not in entity_id:
                return
            cause = "automation" if kind == "automation_triggered" else "script"
            if context.get("id"):  # HA runs the automation's actions in this context [UNVERIFIED-ON-HW]
                self._contexts[context["id"]] = (cause, entity_id)
                while len(self._contexts) > CONTEXTS:
                    self._contexts.popitem(last=False)
            self._add(
                {
                    "ts": ts,
                    "kind": "trigger",
                    "entity_id": entity_id,
                    "state": None,
                    "cause": cause,
                    "ref": entity_id,
                }
            )
        elif kind == predict.ASK_EVENT:
            action = data.get("action")
            if isinstance(action, str) and action.startswith(ASK_PREFIX):
                self._add(
                    {
                        "ts": ts,
                        "kind": "answer",
                        "entity_id": ANSWER_ENTITY,
                        "state": action[:MAX_STATE],
                        "cause": "user",
                        "ref": None,
                    }
                )

    def flush_numeric(self, now: float) -> None:
        """The last value of each numeric sensor, once its minute is over."""
        for entity_id, (ts, state) in list(self._numeric.items()):
            if int(ts // 60) < int(now // 60):
                del self._numeric[entity_id]
                self._add(
                    {
                        "ts": ts,
                        "kind": "state",
                        "entity_id": entity_id,
                        "state": state,
                        "cause": "physical",
                        "ref": None,
                    }
                )

    # --- the tick ---------------------------------------------------------------------------------
    async def tick(self) -> None:
        now = clock.time()
        self.flush_numeric(now)
        if self.ha.connected:
            if now - self._programs_at > 3600:
                self._programs_at = now
                with contextlib.suppress(Exception):
                    self._programs = await self.insights.programs(getattr(self.ha, "own_user_id", None))
            sig = self._signature()
            if sig != self._registry_sig or now - (self.registry_sent or 0) > REGISTRY_S:
                await self.push_registry(sig)
        await self.push()
        if now - self._status_at > STATUS_S:
            await self.refresh_status()
        if self.connected and self._need_backfill and self.ha.connected and not self._backfilling():
            self._need_backfill = False
            self._backfill_task = asyncio.create_task(self.run_backfill(), name="learner-backfill")
        if now - self._model_at > MODEL_S:
            await self.fetch_model()
        self._expire_tables(now)

    def _backfilling(self) -> bool:
        return self._backfill_task is not None and not self._backfill_task.done()

    async def _call(self, method: str, path: str, **kw) -> dict | None:
        try:
            response = await self._http.request(method, self.url + path, **kw)
        except httpx.HTTPError as err:
            self.connected, self.error = False, f"{type(err).__name__}: {err}"
            return None
        if response.status_code == 404 and path == "/model":
            self.connected, self.error = True, None
            return None
        if response.status_code >= 400:
            self.error = f"{path}: HTTP {response.status_code} {response.text[:200]}"
            self.connected = response.status_code != 401 and response.status_code < 500
            if response.status_code == 400 and path == "/events":
                return {"rejected": True}
            return None
        self.connected, self.error = True, None
        return response.json()

    async def push(self) -> None:
        if not self.buffer:
            return
        batch = list(islice(self.buffer, BATCH))
        answer = await self._call("POST", "/events", json={"schema_version": 1, "events": batch})
        if answer is None:
            return
        for _ in batch:
            self.buffer.popleft()
        if answer.get("rejected"):
            log.warning("the learner rejected a batch of %d events: %s", len(batch), self.error)
            return
        self.pushed += len(batch)
        self.last_push = clock.time()
        for suggestion in answer.get("suggestions") or []:
            await self.consider(suggestion)

    async def refresh_status(self) -> None:
        self._status_at = clock.time()
        self.jetson = await self._call("GET", "/status")

    # --- the registry -----------------------------------------------------------------------------
    def _signature(self) -> str:
        reg = self.ha.registry
        ours = sorted(self.insights.ours())
        return f"{len(self.ha.states)}|{sorted(reg.areas)}|{hash(frozenset(reg.entity_area.items()))}|{ours}"

    def registry_doc(self) -> dict:
        reg = self.ha.registry
        entities = []
        for entity_id, state in self.ha.states.items():
            attrs = state.get("attributes") or {}
            meta = reg.entity_meta.get(entity_id, {})
            entities.append(
                {
                    "entity_id": entity_id,
                    "area_id": reg.area_of(entity_id),
                    "name": reg.name_of(entity_id),
                    "state": str(state.get("state"))[:MAX_STATE],
                    "attributes": {
                        k: attrs[k]
                        for k in ("friendly_name", "device_class", "unit_of_measurement")
                        if k in attrs
                    },
                    "registry": {
                        k: meta[k]
                        for k in ("disabled_by", "hidden_by", "entity_category", "platform")
                        if meta.get(k) is not None
                    },
                }
            )
        ours = {
            entity: {
                "ha_id": info["ha_id"],
                "ask": predict.ASK_EVENT in (info.get("yaml") or ""),
                "targets": [list(t) for t in sorted(predict.acted_states(info.get("yaml") or ""))],
            }
            for entity, info in self.insights.ours().items()
        }
        return {
            "schema_version": 1,
            "ts": clock.time(),
            "time_zone": getattr(self.ha, "time_zone", None),
            "areas": [{"area_id": a, "name": (info or {}).get("name")} for a, info in reg.areas.items()],
            "entities": entities,
            "ours": ours,
            "policy": self.safety.policy,
            "settings": {
                "habit_days": int(getattr(self.runtime, "habit_days", 14)),
                "habit_min_repeats": int(getattr(self.runtime, "habit_min_repeats", 3)),
            },
        }

    async def push_registry(self, sig: str | None = None) -> None:
        if await self._call("PUT", "/registry", json=self.registry_doc()) is not None:
            self.registry_sent, self._registry_sig = clock.time(), sig or self._signature()

    # --- backfill ---------------------------------------------------------------------------------
    async def run_backfill(self) -> None:
        """Fill the journal from HA's logbook: from its newest event (or `BACKFILL_DAYS` back) up to
        the oldest event still in the buffer, day by day."""
        status = await self._call("GET", "/status")
        if status is None:
            self._need_backfill = True
            return
        end = self.buffer[0]["ts"] if self.buffer else (self._live_since or clock.time())
        last = (status.get("journal") or {}).get("last_ts")
        start = max(float(last), end - BACKFILL_DAYS * 86400) if last else end - BACKFILL_DAYS * 86400
        if end - start < 60:
            self.backfill = {"state": "done", "from": start, "to": end, "days": 0, "events": 0}
            return
        self.backfill = {"state": "running", "from": start, "to": end, "days": 0, "events": 0, "error": None}
        own = getattr(self.ha, "own_user_id", None)
        try:
            day = start
            while day < end:
                stop = min(end, day + 86400)
                a, b = (datetime.fromtimestamp(x, UTC).isoformat() for x in (day, stop))
                entries = await self.ha.logbook(a, b)
                events = self._from_logbook(entries, own, day, stop)
                for i in range(0, len(events), BATCH):
                    chunk = events[i : i + BATCH]
                    answer = await self._call(
                        "POST", "/events", json={"schema_version": 1, "backfill": True, "events": chunk}
                    )
                    if answer is None:
                        raise RuntimeError(self.error or "the learner did not answer")
                    self.backfill["events"] += len(chunk)
                self.backfill["days"] += 1
                day = stop
            self.backfill["state"] = "done"
            await self.refresh_status()
            done = f"{self.backfill['events']} events of {self.backfill['days']} day(s)"
            self.feed.add("system", f"Learner: filled {done} from Home Assistant's logbook")
        except Exception as err:  # noqa: BLE001 - try again on the next contact
            self.backfill.update(state="failed", error=f"{type(err).__name__}: {err}")
            self._need_backfill = True

    def _from_logbook(self, entries: list[dict], own: str | None, start: float, end: float) -> list[dict]:
        actions = [
            {"ts": r["ts"], "agent_id": r["agent_id"], "entity_id": json.loads(r["entity_ids"])[0]}
            for r in self.store.query(
                "SELECT ts, agent_id, entity_ids FROM actions WHERE ts>=? AND ts<? "
                "AND verdict IN ('executed', 'approved')",
                (start, end),
            )
        ]
        events = [
            {
                "ts": c.ts,
                "kind": "state",
                "entity_id": c.entity_id,
                "state": c.state[:MAX_STATE],
                "cause": c.cause,
                "ref": c.ref,
            }
            for c in changes_from_logbook(entries, own, actions, self._programs)
            if "." in c.entity_id
        ]
        for entity_id, times in fires_from_logbook(entries).items():
            events += [
                {
                    "ts": t,
                    "kind": "trigger",
                    "entity_id": entity_id,
                    "state": None,
                    "cause": "automation",
                    "ref": entity_id,
                }
                for t in times
            ]
        return sorted(events, key=lambda e: e["ts"])

    # --- the model --------------------------------------------------------------------------------
    async def fetch_model(self) -> None:
        self._model_at = clock.time()
        model = await self._call("GET", "/model")
        if not model:
            return
        self.store.set("learner_model", model)
        if clock.time() - float(model.get("trained_at") or 0) <= FRESH_S:
            self.store.set("learner_usual_states", model.get("usual_states") or {})
            self.store.set("learner_event_relevance", model.get("event_relevance") or {})
        self._timeout_findings(model)

    def model(self) -> dict | None:
        """The stored house model while it is fresh."""
        model = self.store.get("learner_model")
        if not model or clock.time() - float(model.get("trained_at") or 0) > FRESH_S:
            return None
        return model

    def _expire_tables(self, now: float) -> None:
        model = self.store.get("learner_model")
        if model and now - float(model.get("trained_at") or 0) > FRESH_S:
            for key in ("learner_usual_states", "learner_event_relevance"):
                if self.store.get(key):
                    self.store.set(key, {})

    def habits(self) -> tuple[list[dict], dict] | None:
        """The D35 predictions over the learner's longer history, for the history scan."""
        model = self.model()
        habits = (model or {}).get("habits") or {}
        if not habits.get("predictions") and not habits.get("funnel"):
            return None
        return [dict(p) for p in habits.get("predictions") or []], dict(habits.get("funnel") or {})

    def _timeout_findings(self, model: dict) -> None:
        ours = {info["ha_id"]: entity for entity, info in self.insights.ours().items()}
        by_entity = {entity: ha_id for ha_id, entity in ours.items()}
        for item in model.get("timeouts") or []:
            if item.get("corrections_per_day", 0) < TIMEOUT_MIN_CORRECTIONS:
                continue
            if abs(item["suggested_min"] - item["current_min"]) < 2:
                continue
            entity, source = item["entity_id"], item["source"]
            name = self.ha.registry.name_of(entity) or entity
            sensors = ", ".join(item.get("presence") or [])
            ha_id = by_entity.get(source)
            title = f"{name}: switched off too early ({item['current_min']:g} → {item['suggested_min']} min)"
            request = (
                f"{source} switches {entity} off about {item['current_min']:g} min after {sensors} went "
                f"quiet. The owner switched it back on {item['corrections_per_day']:g} times a day: the "
                f"sensor drops out while someone sits still. Over the learner's history a timeout of "
                f"{item['suggested_min']} min would have avoided most of that (cost a day "
                f"{item['cost_now']:g} → {item['cost_suggested']:g}: wrong offs against empty lit minutes). "
                f"Write an automation that switches {entity} off when {sensors} have all been off for "
                f"{item['suggested_min']} minutes."
            )
            if not ha_id:
                request += f" It replaces what {source} does: the owner switches that off themselves."
            data = {**item, "name": name}
            self.insights._save(
                f"timeout:{entity}:{source}",
                "timeout",
                title[:200],
                request,
                data,
                self.ha.registry.area_of(entity),
                ha_id,
            )

    # --- live suggestions ---------------------------------------------------------------------------
    def targets(self) -> list[str]:
        """Entities the learner may act on: devices among its live targets of a fresh model. Never a
        helper (the house mode is the owner's and the orchestrator's; guests / work only the owner's):
        the Jetson names the targets, so the add-on decides which kinds it accepts."""
        model = self.model() or {}
        return sorted(
            {
                m["entity_id"]
                for m in model.get("live") or []
                if isinstance(m.get("entity_id"), str) and m["entity_id"].split(".", 1)[0] in LIVE_DOMAINS
            }
        )

    async def consider(self, s: dict) -> dict | None:
        if self.actuator is None or s.get("mode") != "act" or not s.get("earned"):
            return None
        entity_id, state = s.get("entity_id"), s.get("state")
        if not isinstance(entity_id, str) or not isinstance(state, str) or entity_id not in self.targets():
            return None
        key, now = (entity_id, state), clock.time()
        if now - self._acted.get(key, -1e18) < predict.SUPPRESS_S:
            return None
        if (self.ha.states.get(entity_id) or {}).get("state") == state:
            return None
        call = predict.service(entity_id, state)
        if call is None:
            return None
        self._acted[key] = now
        service, data = call
        domain, _, name = service.partition(".")
        action = await self.actuator.request(
            LEARNER, domain, name, entity_id, data, force_shadow=self.mode != "live"
        )
        recent = (self.store.get("learner_actions") or [])[-49:]
        recent.append(
            {
                "ts": now,
                "entity_id": entity_id,
                "state": state,
                "p": s.get("p"),
                "verdict": action["verdict"],
                "action_id": action["id"],
            }
        )
        self.store.set("learner_actions", recent)
        return action

    # --- for people and the orchestrator -----------------------------------------------------------
    def facts(self, room: str | None = None, limit: int = FACTS) -> list[str]:
        """What the learner knows, as short lines (counts, no history). `room`: only that area."""
        model = self.model()
        if not model:
            return []
        area = self.ha.registry.area_of

        def here(*entities: str) -> bool:
            return room is None or any(area(e) == room for e in entities)

        out = []
        for p in (model.get("habits") or {}).get("predictions") or []:
            if here(p["entity_id"]):
                record = f"right {p.get('right')} of {p.get('fires')} on {p.get('right_days')} days"
                out.append(f"habit: {p['entity_id']} → {p['state']} ({p.get('rule') or ''}; {record})")
        for r in model.get("routines") or []:
            if here(r["entity_id"]):
                out.append(
                    f"routine: {r['entity_id']} → {r['state']} on {r['days']} at {r['at']} "
                    f"(±{r['spread_min']:g} min, {r['seen_days']} days)"
                )
        for d in model.get("dependencies") or []:
            a, b = d["after"], d["then"]
            if here(a["entity_id"], b["entity_id"]):
                how = f"after ~{d['lag_s']:g} s ({d['count']} times, {d['lift']:g}× chance)"
                out.append(
                    f"follows: {a['entity_id']} → {a['state']}, then by hand "
                    f"{b['entity_id']} → {b['state']} {how}"
                )
        for t in model.get("timeouts") or []:
            if here(t["entity_id"]):
                out.append(
                    f"timeout: {t['source']} switches {t['entity_id']} off {t['current_min']:g} min after "
                    f"its room went quiet; corrections ask for {t['suggested_min']} min"
                )
        for c in model.get("corrections") or []:
            if here(c["entity_id"]):
                out.append(
                    f"corrected: {c['source']} sets {c['entity_id']} → {c['state']}, a person "
                    f"changed it back {c['corrected']} of {c['fires']} times"
                )
        return out[:limit]

    def status(self) -> dict:
        model = self.store.get("learner_model")
        today = clock.time() - 86400
        recent = [a for a in self.store.get("learner_actions") or [] if a["ts"] >= today]
        summary = None
        if model:
            summary = {
                "trained_at": model.get("trained_at"),
                "history": model.get("history"),
                "fresh": self.model() is not None,
                "counts": {
                    "habits": len((model.get("habits") or {}).get("predictions") or []),
                    **{
                        k: len(model.get(k) or [])
                        for k in ("live", "routines", "dependencies", "corrections")
                    },
                    "timeouts": len(model.get("timeouts") or []),
                },
                "facts": self.facts(limit=20),
                "health": model.get("health"),
                "asks": model.get("asks"),
            }
        return {
            "configured": self.configured,
            "mode": self.mode,
            "link": {
                "connected": self.connected,
                "last_push": self.last_push,
                "pushed": self.pushed,
                "buffered": len(self.buffer),
                "dropped": self.dropped,
                "error": self.error,
                "backfill": self.backfill,
                "registry_sent": self.registry_sent,
            },
            "jetson": self.jetson,
            "model": summary,
            "live": {
                "acted": sum(1 for a in recent if a["verdict"] in ("executed", "approved")),
                "shadow": sum(1 for a in recent if a["verdict"] == "shadow"),
                "denied": sum(1 for a in recent if a["verdict"] == "denied"),
                "recent": recent[-10:],
            },
        }

    async def train(self) -> dict | None:
        return await self._call("POST", "/train")
