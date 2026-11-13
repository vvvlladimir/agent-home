"""Orchestrator: drafts automations for the owner, on the local model or in the cloud.

Providers (add-on option `orchestrator_provider`, D11):
- `local`     — llama-server on the Jetson only;
- `anthropic` — the Claude API only (`anthropic_model`, default `claude-haiku-5-5`);
- `auto`      — local first; the cloud when the local model is unreachable or fails a turn.

Cloud calls carry redacted text only (`redact.py`, unless `redact_cloud` is off) and are paid
from a daily budget in EUR with a hard stop: a call whose worst case (prompt + `max_tokens`) would
cross the limit is not made. Spend is stored per day in the `spend` table.

The orchestrator never touches Home Assistant: what it writes is a proposal (`lifecycle.create`),
checked like any other and deployed only when the owner approves it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime

from .actuator import new_id
from .agents import is_noise
from .lifecycle import LifecycleError
from .llm import LLMError, Priority
from .policy import READ_ONLY_DOMAINS, resolve_tier
from .redact import Redactor

log = logging.getLogger(__name__)

# USD per million tokens (input, output), RESEARCH §6. Unknown models are priced like the most
# expensive one, so a typo in the model name cannot overspend the budget.
PRICES_USD = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-2": (8.0, 40.0),
}
FALLBACK_PRICE = (8.0, 40.0)
EUR_PER_USD = 0.92  # fixed; the budget is an approximate guard, not accounting
# Server-side refusal fallbacks exist for these models, not for Haiku (RESEARCH §6).
FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 2048
MAX_TURNS = 4
MAX_ENTITY_LINES = 80
MAX_QUEUED_DRAFTS = 3
MAX_AGENT_DRAFTS_PER_DAY = 3  # per room agent; the owner's own requests are not capped

SYSTEM = """You are the orchestrator of Agent House, an assistant that writes Home Assistant \
automations for the owner of a smart home. The owner reviews every automation before it runs.

How to answer:
- Call submit_automation with one automation in YAML. You get the result of the safety checks;
  if a check failed, fix the YAML and call submit_automation again.
- If the request cannot be done safely or with the listed entities, call decline with the reason.

YAML rules:
- Keys: alias, description, triggers, conditions, actions, mode. Plural keys only.
- Triggers use `trigger:` (state, numeric_state, time, sun, …). Actions use `action: domain.service`.
- Name every target entity explicitly: `target: {entity_id: light.x}`. No area_id, device_id,
  label_id, no templates in entity ids or service names.
- Use only entity ids from the list in the request, and only these services:
{services}
  plus notify.<target> and persistent_notification.create for messages.
- Entities marked T3 can never be in actions. No `event:` steps, no scripts, no automation.* or
  homeassistant.* services, no webhook triggers.
- Keep it as small as the request allows."""

TOOLS = [
    {
        "name": "submit_automation",
        "description": "Submit one Home Assistant automation for the owner's review.",
        "properties": {
            "title": {"type": "string", "description": "short title for the owner"},
            "rationale": {"type": "string", "description": "one or two sentences: why this automation"},
            "expected_triggers": {"type": "string", "description": "when it should fire, in words"},
            "yaml": {"type": "string", "description": "the automation as YAML"},
        },
        "required": ["title", "rationale", "expected_triggers", "yaml"],
    },
    {
        "name": "decline",
        "description": "Explain why the request cannot be turned into a safe automation.",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
    },
]


class OrchestratorError(Exception):
    """`code`: unavailable | budget | kill_switch | declined | failed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class ProviderError(Exception):
    pass


class BudgetExhausted(ProviderError):
    pass


@dataclass
class Turn:
    text: str = ""
    calls: list[dict] = field(default_factory=list)  # {id, name, input}
    stop_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    eur: float = 0.0
    provider: str = ""
    model: str | None = None
    raw: list | None = None  # Anthropic content blocks, passed back unchanged (thinking blocks)


def price_eur(model: str, tokens_in: float, tokens_out: float) -> float:
    price_in, price_out = PRICES_USD.get(model, FALLBACK_PRICE)
    return (tokens_in * price_in + tokens_out * price_out) / 1e6 * EUR_PER_USD


# --- budget -----------------------------------------------------------------------------------
class Budget:
    def __init__(self, store, settings) -> None:
        self.store, self.settings = store, settings

    @staticmethod
    def day() -> str:
        return datetime.now().astimezone().date().isoformat()

    def today(self) -> dict:
        row = self.store.query(
            "SELECT COALESCE(SUM(eur), 0) AS eur, COALESCE(SUM(tokens_in), 0) AS tin, "
            "COALESCE(SUM(tokens_out), 0) AS tout FROM spend WHERE day=? AND provider='anthropic'",
            (self.day(),),
        )[0]
        limit = float(self.settings.daily_budget_eur)
        return {
            "daily_limit_eur": limit,
            "spent_today_eur": round(row["eur"], 4),
            "tokens_in_today": int(row["tin"]),
            "tokens_out_today": int(row["tout"]),
            "exhausted": row["eur"] >= limit,
        }

    def check(self, worst_case_eur: float) -> None:
        """Raise unless a call costing at most `worst_case_eur` stays within today's limit."""
        state = self.today()
        if state["spent_today_eur"] + worst_case_eur > state["daily_limit_eur"]:
            raise BudgetExhausted(
                f"daily cloud budget: {state['spent_today_eur']:.4f} of {state['daily_limit_eur']:.2f} EUR "
                f"spent, this call could cost up to {worst_case_eur:.4f} EUR"
            )

    def reserve(self, worst_case_eur: float) -> None:
        """check() and book the worst case in one step (no await in between)."""
        self.check(worst_case_eur)
        self.add("anthropic", "(reserved)", 0, 0, worst_case_eur)

    def release(self, reserved_eur: float) -> None:
        self.add("anthropic", "(reserved)", 0, 0, -reserved_eur)

    def add(self, provider: str, model: str, tokens_in: int, tokens_out: int, eur: float) -> None:
        self.store.execute(
            "INSERT INTO spend (day, provider, model, tokens_in, tokens_out, eur) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (day, provider, model) DO UPDATE SET tokens_in=tokens_in+excluded.tokens_in, "
            "tokens_out=tokens_out+excluded.tokens_out, eur=eur+excluded.eur",
            (self.day(), provider, model or "?", tokens_in, tokens_out, eur),
        )


# --- providers --------------------------------------------------------------------------------
class LocalProvider:
    name = "local"

    def __init__(self, llm) -> None:
        self.llm = llm

    @property
    def available(self) -> bool:
        return self.llm.state in ("ok", "busy", "warming")

    async def turn(self, system: str, transcript: list[dict], tools: list[dict], priority: Priority) -> Turn:
        messages: list[dict] = [{"role": "system", "content": system}]
        for item in transcript:
            if item["role"] == "user":
                messages.append({"role": "user", "content": item["text"]})
            elif item["role"] == "assistant":
                entry: dict = {"role": "assistant", "content": item.get("text") or ""}
                if item.get("calls"):
                    entry["tool_calls"] = [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c["input"], ensure_ascii=False),
                            },
                        }
                        for c in item["calls"]
                    ]
                messages.append(entry)
            else:
                messages += [
                    {"role": "tool", "tool_call_id": r["id"], "content": r["content"]}
                    for r in item["results"]
                ]
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": {
                        "type": "object",
                        "properties": t["properties"],
                        "required": t["required"],
                    },
                },
            }
            for t in tools
        ]
        try:
            result = await self.llm.chat(messages, openai_tools, max_tokens=MAX_TOKENS, priority=priority)
        except LLMError as err:
            raise ProviderError(f"local model: {err}") from err
        calls = [
            {"id": c.id, "name": c.name, "input": c.arguments, "error": c.error} for c in result.tool_calls
        ]
        return Turn(
            text=result.text,
            calls=calls,
            stop_reason=result.finish_reason,
            tokens_in=int(result.timings.get("prompt_tokens") or result.timings.get("prompt_n") or 0),
            tokens_out=int(result.timings.get("predicted_n") or 0),
            provider="local",
            model=result.model,
        )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings, budget: Budget) -> None:
        self.settings, self.budget = settings, budget
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.settings.anthropic_api_key) and not self.budget.today()["exhausted"]

    def client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic  # imported lazily: the add-on starts without it in use

            self._client = AsyncAnthropic(
                api_key=self.settings.anthropic_api_key,
                base_url=self.settings.anthropic_base_url or None,
                max_retries=0,  # a retry is a second bill; `auto` falls back to the local model
                timeout=90.0,
            )
        return self._client

    @staticmethod
    def _messages(transcript: list[dict]) -> list[dict]:
        out: list[dict] = []
        for item in transcript:
            if item["role"] == "user":
                out.append({"role": "user", "content": item["text"]})
            elif item["role"] == "assistant":
                if item.get("raw"):
                    out.append({"role": "assistant", "content": item["raw"]})
                    continue
                blocks: list[dict] = [{"type": "text", "text": item["text"]}] if item.get("text") else []
                blocks += [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                    for c in item.get("calls") or []
                ]
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]})
            else:
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": r["id"],
                                "content": r["content"],
                                "is_error": bool(r.get("is_error")),
                            }
                            for r in item["results"]
                        ],
                    }
                )
        return out

    async def turn(self, system: str, transcript: list[dict], tools: list[dict], priority: Priority) -> Turn:
        import anthropic

        if not self.settings.anthropic_api_key:
            raise ProviderError("no anthropic_api_key configured")
        model = self.settings.anthropic_model
        request = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": self._messages(transcript),
            "tools": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "strict": True,
                    "input_schema": {
                        "type": "object",
                        "properties": t["properties"],
                        "required": t["required"],
                        "additionalProperties": False,
                    },
                }
                for t in tools
            ],
            "output_config": {"effort": "medium"},
        }
        estimate_in = math.ceil(len(json.dumps(request, ensure_ascii=False)) / 3.0)
        # Reserve the worst case before the call (a server-side fallback may answer with a
        # pricier model): concurrent drafts cannot all pass the check, and a call that fails
        # half-way stays charged at its worst case.
        worst = price_eur(model, estimate_in, MAX_TOKENS)
        if model in FALLBACK_MODELS:
            worst += price_eur("", estimate_in, MAX_TOKENS)
        self.budget.reserve(worst)
        try:
            if model in FALLBACK_MODELS:
                response = await self.client().beta.messages.create(
                    **request, betas=[FALLBACK_BETA], fallbacks="default"
                )
            else:
                response = await self.client().messages.create(**request)
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as err:
            raise ProviderError(f"Claude API: {type(err).__name__}: {err}"[:300]) from err
        usage = response.usage
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        tokens_in = usage.input_tokens + cache_write + cache_read
        billed_in = usage.input_tokens + 1.25 * cache_write + 0.1 * cache_read
        eur = price_eur(response.model or model, billed_in, usage.output_tokens)
        if response.model and response.model != model:  # a fallback ran: the declined attempt counts too
            eur += price_eur(model, billed_in, usage.output_tokens)
        self.budget.add("anthropic", response.model or model, tokens_in, usage.output_tokens, eur)
        self.budget.release(worst)
        if response.stop_reason == "refusal":
            raise ProviderError("the Claude API declined the request (refusal)")
        raw = [block.model_dump(mode="json", exclude_none=True) for block in response.content]
        return Turn(
            text="".join(b.text for b in response.content if b.type == "text"),
            calls=[
                {"id": b.id, "name": b.name, "input": b.input}
                for b in response.content
                if b.type == "tool_use"
            ],
            stop_reason=response.stop_reason,
            tokens_in=tokens_in,
            tokens_out=usage.output_tokens,
            eur=eur,
            provider="anthropic",
            model=response.model,
            raw=raw,
        )


# --- orchestrator -----------------------------------------------------------------------------
class Orchestrator:
    def __init__(self, settings, llm, store, feed, ha, safety, lifecycle, agents_rt=None) -> None:
        self.settings, self.store, self.feed, self.ha, self.safety = settings, store, feed, ha, safety
        self.lifecycle, self.agents_rt = lifecycle, agents_rt
        self.budget = Budget(store, settings)
        self.local = LocalProvider(llm)
        self.cloud = AnthropicProvider(settings, self.budget)
        self._tasks: set[asyncio.Task] = set()

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- status -----------------------------------------------------------------------------
    def providers(self) -> list:
        return {
            "local": [self.local],
            "anthropic": [self.cloud],
        }.get(self.settings.orchestrator_provider, [self.local, self.cloud])

    def status(self) -> dict:
        usable = [p for p in self.providers() if p.available]
        effective = usable[0].name if usable else "none"
        return {
            "provider": self.settings.orchestrator_provider,
            "effective": effective,
            "model": self.settings.anthropic_model if effective == "anthropic" else self.local.llm.model,
            "budget": self.budget.today(),
        }

    # --- one model turn, with fallback ------------------------------------------------------
    async def turn(
        self, system: str, transcript: list[dict], tools: list[dict], priority: Priority, redactor: Redactor
    ) -> Turn:
        errors = []
        for provider in self.providers():
            if self.safety.kill_engaged:
                raise OrchestratorError("kill_switch", "the kill switch is engaged")
            if provider is self.cloud and self.settings.redact_cloud:
                sent_system = redactor.redact(system)
                sent = [self._redact_item(item, redactor) for item in transcript]
            else:
                sent_system, sent = system, transcript
            try:
                turn = await provider.turn(sent_system, sent, tools, priority)
            except BudgetExhausted as err:
                errors.append(str(err))
                continue
            except ProviderError as err:
                log.warning("orchestrator provider %s failed: %s", provider.name, err)
                errors.append(str(err))
                continue
            if provider is self.cloud and self.settings.redact_cloud:
                turn.calls = [{**c, "input": redactor.restore(c["input"])} for c in turn.calls]
            return turn
        if any("budget" in e for e in errors) and len(errors) == len(self.providers()):
            raise OrchestratorError("budget", "; ".join(errors))
        raise OrchestratorError("unavailable", "; ".join(errors) or "no provider is configured")

    @staticmethod
    def _redact_item(item: dict, redactor: Redactor) -> dict:
        if item["role"] == "user":
            return {**item, "text": redactor.redact(item["text"])}
        if item["role"] == "tool":
            return {
                **item,
                "results": [{**r, "content": redactor.redact(r["content"])} for r in item["results"]],
            }
        if item.get("raw"):
            return item  # the cloud's own answer: written from redacted text, thinking signatures intact
        return {
            **item,
            "text": redactor.redact(item.get("text") or ""),
            "calls": [{**c, "input": redactor.redact_value(c["input"])} for c in item.get("calls") or []],
        }

    # --- drafting ---------------------------------------------------------------------------
    def entity_lines(self, request: str, room: str | None) -> list[str]:
        """The entities the model may use: the room's (or the house's), most relevant first."""
        policy = self.safety.policy
        words = {w for w in request.lower().replace(".", " ").replace("_", " ").split() if len(w) > 2}
        rows = []
        for entity_id, state in self.ha.states.items():
            area = self.ha.registry.area_of(entity_id)
            if area is None or (room and area != room) or is_noise(self.ha, entity_id):
                continue
            if self.ha.registry.is_hidden(entity_id) or self.ha.registry.entity_meta.get(entity_id, {}).get(
                "entity_category"
            ):
                continue
            domain = entity_id.split(".", 1)[0]
            tier = resolve_tier(entity_id, policy)
            if domain not in READ_ONLY_DOMAINS and domain not in (policy.get("allowed_services") or {}):
                continue
            name = str((state.get("attributes") or {}).get("friendly_name") or "")
            haystack = f"{entity_id} {name} {area}".lower().replace("_", " ")
            score = sum(w in haystack for w in words)
            rows.append(
                (
                    -score,
                    tier == "T3",
                    entity_id,
                    f"- {entity_id} ({name[:40]}, {area}) [{tier}] = {state.get('state')}",
                )
            )
        rows.sort()
        return [line for *_, line in rows[:MAX_ENTITY_LINES]]

    def _system(self) -> str:
        services = self.safety.policy.get("allowed_services") or {}
        listed = "\n".join(f"  {d}: {', '.join(s)}" for d, s in sorted(services.items()))
        return SYSTEM.replace("{services}", listed)

    async def draft(
        self,
        request: str,
        *,
        room: str | None = None,
        origin_agent: str | None = None,
        run_id: str | None = None,
        ha_id: str | None = None,
        priority: Priority = Priority.INTERACTIVE,
    ) -> dict:
        """Ask the model for an automation; return the proposal (draft or blocked). Never deploys."""
        if self.safety.kill_engaged:
            raise OrchestratorError("kill_switch", "the kill switch is engaged")
        request = request.strip()[:2000]
        lines = self.entity_lines(request, room)
        current = ""
        if ha_id:
            deployed = self.lifecycle.get_deployed(ha_id)
            current = "\n\nChange this deployed automation:\n" + deployed["versions"][0]["yaml"]
        user = f"Request: {request}{current}\n\nEntities (id, name, area, tier, state):\n" + (
            "\n".join(lines) or "(none)"
        )
        transcript: list[dict] = [{"role": "user", "text": user}]
        trace = _Trace(self, origin_agent, run_id, request, priority)
        trace.step("context", {"system": self._system(), "user": user, "entities": len(lines)})
        redactor = Redactor.for_home(self.ha)
        proposal = None
        try:
            for _ in range(MAX_TURNS):
                turn = await self.turn(self._system(), transcript, TOOLS, priority, redactor)
                trace.llm(turn)
                if not turn.calls:
                    if proposal is not None:
                        break
                    transcript += [
                        {"role": "assistant", "text": turn.text or "…", "raw": turn.raw},
                        {"role": "user", "text": "Call submit_automation or decline."},
                    ]
                    continue
                results, done = [], False
                for call in turn.calls:
                    if call.get("error") or call["name"] not in ("submit_automation", "decline"):
                        results.append(
                            {
                                "id": call["id"],
                                "content": f"unusable call: {call.get('error') or call['name']}",
                                "is_error": True,
                            }
                        )
                        continue
                    if call["name"] == "decline":
                        reason = str(call["input"].get("reason") or "no reason given")[:300]
                        trace.finish("escalated", error=f"declined: {reason}")
                        raise OrchestratorError("declined", reason)
                    args = call["input"]
                    text = str(args.get("yaml") or "")
                    if proposal is None:
                        proposal = await self.lifecycle.create(
                            text,
                            title=args.get("title"),
                            rationale=args.get("rationale"),
                            expected_triggers=args.get("expected_triggers"),
                            origin_agent=origin_agent,
                            run_id=trace.run_id,
                            ha_id=ha_id,
                        )
                    else:
                        proposal = await self.lifecycle.edit(proposal["id"], text)
                    failed = [f"{c['name']}: {c['detail']}" for c in proposal["checks"] if not c["ok"]]
                    trace.step(
                        "tool",
                        {
                            "name": "submit_automation",
                            "proposal_id": proposal["id"],
                            "status": proposal["status"],
                            "failed": failed,
                        },
                    )
                    done = not failed
                    results.append(
                        {
                            "id": call["id"],
                            "content": "All checks passed; the owner will review it. Reply with one sentence."
                            if done
                            else "Checks failed, fix and submit again:\n" + "\n".join(failed),
                            "is_error": not done,
                        }
                    )
                transcript += [
                    {"role": "assistant", "text": turn.text, "calls": turn.calls, "raw": turn.raw},
                    {"role": "tool", "results": results},
                ]
                if done:
                    break
        except OrchestratorError as err:
            trace.finish("error" if err.code != "declined" else "escalated", error=str(err))
            raise
        except LifecycleError as err:
            trace.finish("error", error=str(err))
            raise OrchestratorError("failed", str(err)) from err
        if proposal is None:
            trace.finish("escalated", error="the model never submitted an automation")
            raise OrchestratorError("failed", "the model never submitted an automation")
        trace.finish(
            "ok" if proposal["status"] == "draft" else "escalated",
            summary=f"proposal {proposal['id']} ({proposal['status']}): {proposal['title']}",
        )
        return proposal

    def request_draft(self, agent_id: str, run_id: str | None, description: str, room: str | None) -> bool:
        """A room agent's idea, drafted in the background. False when too many are waiting."""
        if len(self._tasks) >= MAX_QUEUED_DRAFTS or self.safety.kill_engaged:
            return False
        today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        drafted = self.store.query(
            "SELECT COUNT(*) AS n FROM runs WHERE agent_id='orchestrator' AND started>=? "
            "AND wake_reason LIKE ?",
            (today, f"{agent_id} asks for an automation:%"),
        )[0]["n"]
        if drafted >= MAX_AGENT_DRAFTS_PER_DAY:
            return False

        async def job() -> None:
            try:
                await self.draft(
                    description, room=room, origin_agent=agent_id, run_id=run_id, priority=Priority.BACKGROUND
                )
            except OrchestratorError as err:
                self.feed.add(
                    "proposal",
                    f"{agent_id}: no proposal drafted ({err})",
                    level="warning",
                    agent_id=agent_id,
                    run_id=run_id,
                )
            except Exception:  # noqa: BLE001
                log.exception("background draft failed")

        task = asyncio.get_running_loop().create_task(job(), name=f"draft-{agent_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True


class _Trace:
    """An orchestrator draft as a run (agent_id `orchestrator`), so Runs & traces shows it."""

    def __init__(
        self, orch: Orchestrator, origin_agent, origin_run, request: str, priority: Priority
    ) -> None:
        self.store = orch.store
        self.run_id = new_id("r")
        self.started = time.time()
        self.trace: list[dict] = []
        self.steps = self.tokens_in = self.tokens_out = 0
        self.provider = None
        source = "manual" if priority is Priority.INTERACTIVE else "poke"
        reason = f"{origin_agent or 'owner'} asks for an automation: {request}"[:500]
        self.store.execute(
            "INSERT INTO runs (id, agent_id, status, wake_source, wake_reason, started, shadow, replay_of) "
            "VALUES (?, 'orchestrator', 'running', ?, ?, ?, 1, NULL)",
            (self.run_id, source, reason, self.started),
        )
        if origin_run:
            self.step("origin", {"run_id": origin_run, "agent_id": origin_agent})

    def step(self, kind: str, data: dict, **extra) -> None:
        self.trace.append(
            {
                "idx": len(self.trace),
                "ts": time.time(),
                "type": kind,
                "data": data,
                **{k: extra.get(k) for k in ("latency_ms", "tokens_in", "tokens_out")},
            }
        )
        self.store.execute(
            "UPDATE runs SET steps=?, tokens_in=?, tokens_out=?, provider=?, trace=? WHERE id=?",
            (
                self.steps,
                self.tokens_in,
                self.tokens_out,
                self.provider,
                json.dumps(self.trace, ensure_ascii=False, default=str),
                self.run_id,
            ),
        )

    def llm(self, turn: Turn) -> None:
        self.steps += 1
        self.tokens_in += turn.tokens_in
        self.tokens_out += turn.tokens_out
        self.provider = turn.provider
        self.step(
            "llm",
            {
                "provider": turn.provider,
                "model": turn.model,
                "content": turn.text,
                "tool_calls": [{"name": c["name"], "arguments": c["input"]} for c in turn.calls],
                "finish_reason": turn.stop_reason,
                "eur": round(turn.eur, 6),
            },
            tokens_in=turn.tokens_in,
            tokens_out=turn.tokens_out,
        )

    def finish(self, status: str, summary: str | None = None, error: str | None = None) -> None:
        ended = time.time()
        self.store.execute(
            "UPDATE runs SET status=?, ended=?, latency_ms=?, summary=?, error=? "
            "WHERE id=? AND ended IS NULL",
            (
                status,
                ended,
                int((ended - self.started) * 1000),
                summary,
                (error or "")[:300] or None,
                self.run_id,
            ),
        )
