"""Orchestrator: drafts automations for the owner, on the local model or in the cloud.

Providers (add-on option `orchestrator_provider`, D11, D16):
- `local`     — llama-server on the Jetson only;
- `anthropic` — the Claude API only (`anthropic_model`, default `claude-haiku-5-5`);
- `openai`    — the OpenAI Responses API only (`openai_model`, default `gpt-6.1-sol`);
- `auto`      — local first; then every cloud provider that has a key (Claude, then OpenAI) when
                the local model is unreachable or fails a turn.

Per task (M5, D21): `routine` work (the daily review's summary) runs on the local model only; the
cloud is the expert for `draft`, `habit`, `undo` and `conflict` drafts. With `auto`, a draft the
local model could not finish (no submission, or a proposal that is still blocked) is tried once
more by the cloud alone — the low-confidence case.

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
from .agents import is_noise, is_room_sensor
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
    # OpenAI, developers.openai.com/api/docs/models (checked 2026-11-13)
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6.1-terra": (1.0, 5.0),
    "gpt-6.1-sol": (2.0, 10.0),
    "gpt-6-astra": (10.0, 50.0),
}
FALLBACK_PRICE = (10.0, 50.0)
EUR_PER_USD = 0.92  # fixed; the budget is an approximate guard, not accounting
# Server-side refusal fallbacks exist for these models, not for Haiku (RESEARCH §6).
FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 2048
OPENAI_MAX_TOKENS = 4096  # includes reasoning tokens on the GPT-6 models
MAX_TURNS = 6
MAX_ENTITY_LINES = 60
FIND_LIMIT = 25
SEARCH_FIRST = (
    "Search with find_entities first (English words, e.g. 'occupancy', 'zone', 'desk'); "
    "decline only if nothing fits."
)
MAX_QUEUED_DRAFTS = 3
MAX_AGENT_DRAFTS_PER_DAY = 3  # per room agent; the owner's own requests are not capped

SYSTEM = """You are the orchestrator of Agent House, an assistant that writes Home Assistant \
automations for the owner of a smart home. The owner reviews every automation before it runs.

How to answer:
- The request lists some entities, not all. Use find_entities (English words such as occupancy,
  zone, desk, lamp, temperature, or an area) to find the sensors and devices you need. Never decline
  because an entity is missing from the list before you have searched for it.
- Call submit_automation with one automation in YAML. You get the result of the safety checks;
  if a check failed, fix the YAML and call submit_automation again.
- If the request cannot be done safely or with the listed entities, call decline with the reason.

{yaml_rules}"""

CHAT_SYSTEM = """You are the orchestrator of Agent House, talking with the owner of a smart home in chat.

How to answer:
- Short and concrete, in the owner's language. Finish with plain text and no tool call.
- You cannot switch anything yourself. Use find_entities (English words, or an area) to look up
  entities and their current states. Describe only states you read; never claim one you did not see.
- When the owner wants something to happen automatically, call submit_automation with one automation
  in YAML; you get the result of the safety checks (fix and submit again if one failed). The owner
  reviews it on the Proposals page; nothing runs before that.

{yaml_rules}"""

YAML_RULES = """YAML rules:
- Keys: alias, description, triggers, conditions, actions, mode. Plural keys only.
- Triggers use `trigger:` (state, numeric_state, time, sun, …). Actions use `action: domain.service`.
- Name every target entity explicitly: `target: {entity_id: light.x}`. No area_id, device_id,
  label_id, no templates in entity ids or service names.
- Use only entity ids from the list in the request, and only these services:
{services}
  plus notify.<target> and persistent_notification.create for messages.
- Entities marked T3 can never be in actions. No `event:` steps, no scripts, no automation.* or
  homeassistant.* services, no webhook triggers.
- The house mode is input_select.agent_house_mode (home, away, night, guests, work). Prefer a
  state condition on it over re-deriving presence or bedtime from sensors.
- Keep it as small as the request allows."""

TOOLS = [
    {
        "name": "find_entities",
        "description": "Search the home's entities by words in their id, name, area or device class. "
        "Returns up to 25 matches with area, tier and state.",
        "properties": {
            "query": {
                "type": "string",
                "description": "space-separated words, all must match, e.g. 'zone occupancy'",
            },
            "area": {"type": "string", "description": "area id to search in, or empty for the whole home"},
        },
        "required": ["query", "area"],
    },
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
CHAT_TOOLS = TOOLS[:2]  # find_entities, submit_automation: a chat answer needs no `decline`
CHAT_HISTORY_TURNS = 8


EXPERT_TASKS = frozenset({"draft", "habit", "undo", "conflict"})
REVIEW_SYSTEM = """You are the orchestrator of Agent House. Once a day you write the owner a short \
review of the house: what the room agents reported, what was escalated, what the history scan found \
and which proposals are being drafted. Plain text, at most 8 short lines, in English, no tool calls. \
Mention conflicts and undone automations first. Do not invent anything that is not in the data."""


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
    raw: list | None = None  # the provider's own output items, passed back unchanged to it only
    raw_by: str | None = None  # (thinking blocks / encrypted reasoning items)


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
            "COALESCE(SUM(tokens_out), 0) AS tout FROM spend WHERE day=? AND provider<>'local'",
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
        self.add("cloud", "(reserved)", 0, 0, worst_case_eur)

    def release(self, reserved_eur: float) -> None:
        self.add("cloud", "(reserved)", 0, 0, -reserved_eur)

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
    def has_key(self) -> bool:
        return bool(self.settings.anthropic_api_key)

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
                if item.get("raw") and item.get("raw_by") == "anthropic":
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
            raw_by="anthropic",
        )


class OpenAIProvider:
    """OpenAI Responses API (the GPT-6 models take tools only there). `store=False`: nothing is
    kept at OpenAI; reasoning items come back encrypted and are passed back unchanged."""

    name = "openai"

    def __init__(self, settings, budget: Budget) -> None:
        self.settings, self.budget = settings, budget
        self._client = None

    @property
    def has_key(self) -> bool:
        return bool(self.settings.openai_api_key)

    @property
    def available(self) -> bool:
        return bool(self.settings.openai_api_key) and not self.budget.today()["exhausted"]

    def client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url or None,
                max_retries=0,  # a retry is a second bill; `auto` falls back instead
                timeout=90.0,
            )
        return self._client

    @staticmethod
    def _input(transcript: list[dict]) -> list[dict]:
        out: list[dict] = []
        for item in transcript:
            if item["role"] == "user":
                out.append({"role": "user", "content": item["text"]})
            elif item["role"] == "assistant":
                if item.get("raw") and item.get("raw_by") == "openai":
                    out += item["raw"]
                    continue
                if item.get("text"):
                    out.append({"role": "assistant", "content": item["text"]})
                out += [
                    {
                        "type": "function_call",
                        "call_id": c["id"],
                        "name": c["name"],
                        "arguments": json.dumps(c["input"], ensure_ascii=False),
                    }
                    for c in item.get("calls") or []
                ]
            else:
                out += [
                    {"type": "function_call_output", "call_id": r["id"], "output": r["content"]}
                    for r in item["results"]
                ]
        return out

    async def turn(self, system: str, transcript: list[dict], tools: list[dict], priority: Priority) -> Turn:
        import openai

        if not self.settings.openai_api_key:
            raise ProviderError("no openai_api_key configured")
        model = self.settings.openai_model
        request = {
            "model": model,
            "instructions": system,
            "input": self._input(transcript),
            "tools": [
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t["description"],
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": t["properties"],
                        "required": t["required"],
                        "additionalProperties": False,
                    },
                }
                for t in tools
            ],
            "max_output_tokens": OPENAI_MAX_TOKENS,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        estimate_in = math.ceil(len(json.dumps(request, ensure_ascii=False)) / 3.0)
        worst = price_eur(model, estimate_in, OPENAI_MAX_TOKENS)
        self.budget.reserve(worst)
        try:
            response = await self.client().responses.create(**request)
        except (openai.APIStatusError, openai.APIConnectionError) as err:
            raise ProviderError(f"OpenAI API: {type(err).__name__}: {err}"[:300]) from err
        usage = response.usage
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        eur = price_eur(response.model or model, usage.input_tokens - 0.9 * cached, usage.output_tokens)
        self.budget.add("openai", response.model or model, usage.input_tokens, usage.output_tokens, eur)
        self.budget.release(worst)
        raw = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
        calls, text, refused = [], [], False
        for item in response.output:
            if item.type == "function_call":
                try:
                    arguments = json.loads(item.arguments or "{}")
                    error = None if isinstance(arguments, dict) else "arguments are not an object"
                except ValueError:
                    arguments, error = {}, "arguments are not valid JSON"
                calls.append({"id": item.call_id, "name": item.name, "input": arguments, "error": error})
            elif item.type == "message":
                for part in item.content or []:
                    if part.type == "output_text":
                        text.append(part.text)
                    elif part.type == "refusal":
                        refused = True
        if refused and not calls:
            raise ProviderError("the OpenAI API declined the request (refusal)")
        return Turn(
            text="".join(text),
            calls=calls,
            stop_reason=response.status,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            eur=eur,
            provider="openai",
            model=response.model,
            raw=raw,
            raw_by="openai",
        )


# --- orchestrator -----------------------------------------------------------------------------
class Orchestrator:
    def __init__(self, settings, llm, store, feed, ha, safety, lifecycle, agents_rt=None) -> None:
        self.settings, self.store, self.feed, self.ha, self.safety = settings, store, feed, ha, safety
        self.lifecycle, self.agents_rt = lifecycle, agents_rt
        self.budget = Budget(store, settings)
        self.local = LocalProvider(llm)
        self.cloud = AnthropicProvider(settings, self.budget)
        self.openai = OpenAIProvider(settings, self.budget)
        self._tasks: set[asyncio.Task] = set()
        self._local_tried = False
        self.council = None  # set by the app factory

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- status -----------------------------------------------------------------------------
    def providers(self, task: str = "draft", expert: bool = False) -> list:
        """The providers to try for one kind of task, in order. `routine` stays on the local model;
        `expert` (a retry after the local model gave up) uses the clouds only."""
        configured = {
            "local": [self.local],
            "anthropic": [self.cloud],
            "openai": [self.openai],
        }.get(self.settings.orchestrator_provider, [self.local, self.cloud, self.openai])
        if task == "routine":
            return [self.local]
        if expert:
            return [p for p in configured if p is not self.local and p.has_key]
        return configured

    def can_ask_expert(self, task: str) -> bool:
        return (
            self.settings.orchestrator_provider == "auto"
            and task in EXPERT_TASKS
            and any(p.available for p in self.providers(task, expert=True))
        )

    def status(self) -> dict:
        usable = [p for p in self.providers() if p.available]
        effective = usable[0].name if usable else "none"
        model = {"anthropic": self.settings.anthropic_model, "openai": self.settings.openai_model}.get(
            effective, self.local.llm.model
        )
        return {
            "provider": self.settings.orchestrator_provider,
            "effective": effective,
            "model": model,
            "budget": self.budget.today(),
        }

    # --- one model turn, with fallback ------------------------------------------------------
    async def turn(
        self, system: str, transcript: list[dict], tools: list[dict], priority: Priority, redactor: Redactor,
        task: str = "draft", expert: bool = False,
    ) -> Turn:  # fmt: skip
        errors = []
        for provider in self.providers(task, expert):
            if self.safety.kill_engaged:
                raise OrchestratorError("kill_switch", "the kill switch is engaged")
            cloud = provider is not self.local
            if cloud and self.settings.orchestrator_provider == "auto" and not provider.has_key:
                continue  # `auto` uses only the clouds the owner gave a key for
            if cloud and self.settings.redact_cloud:
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
            if cloud and self.settings.redact_cloud:
                turn.calls = [{**c, "input": redactor.restore(c["input"])} for c in turn.calls]
            return turn
        if errors and all("budget" in e for e in errors):
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
        # `raw` (the cloud's own output items, written from redacted text; thinking signatures and
        # encrypted reasoning must stay byte-identical) goes back only to the provider that wrote it
        return {
            **item,
            "text": redactor.redact(item.get("text") or ""),
            "calls": [{**c, "input": redactor.redact_value(c["input"])} for c in item.get("calls") or []],
        }

    # --- drafting ---------------------------------------------------------------------------
    def _candidates(self, room: str | None):
        """(entity_id, area, name, tier, state, device_class) the orchestrator may use."""
        policy = self.safety.policy
        services = policy.get("allowed_services") or {}
        for entity_id, state in self.ha.states.items():
            area = self.ha.registry.area_of(entity_id)
            if area is None or (room and area != room) or is_noise(self.ha, entity_id):
                continue
            meta = self.ha.registry.entity_meta.get(entity_id, {})
            if self.ha.registry.is_hidden(entity_id) or meta.get("entity_category"):
                continue
            domain = entity_id.split(".", 1)[0]
            if domain not in READ_ONLY_DOMAINS and domain not in services:
                continue
            attrs = state.get("attributes") or {}
            name = str(attrs.get("friendly_name") or meta.get("name") or meta.get("original_name") or "")
            device_class = attrs.get("device_class") or meta.get("original_device_class") or ""
            yield entity_id, area, name, resolve_tier(entity_id, policy), state.get("state"), device_class

    @staticmethod
    def _line(entity_id, area, name, tier, state, device_class) -> str:
        kind = f", {device_class}" if device_class else ""
        return f"- {entity_id} ({name[:40]}, {area}{kind}) [{tier}] = {state}"

    def entity_lines(self, request: str, room: str | None) -> list[str]:
        """A first look for the model: entities matching the request's words, then room sensors
        (motion, occupancy, …), then devices it may act on. Read-only sensors are T3 by policy but
        are exactly what triggers need, so they are not ranked down. The rest: find_entities."""
        words = {w for w in request.lower().replace(".", " ").replace("_", " ").split() if len(w) > 2}
        rows = []
        for row in self._candidates(room):
            entity_id, area, name, tier, state, device_class = row
            haystack = f"{entity_id} {name} {area} {device_class}".lower().replace("_", " ")
            score = sum(w in haystack for w in words)
            domain = entity_id.split(".", 1)[0]
            if domain in READ_ONLY_DOMAINS:
                group = 1 if is_room_sensor(self.ha, entity_id) else 3
            else:
                group = 0 if tier != "T3" else 4
            unavailable = str(state) in ("unavailable", "unknown")
            rows.append((-score, unavailable, group, entity_id, self._line(*row)))
        rows.sort()
        # matches first; then a fixed share for room sensors (triggers need them), then devices
        picked = [r for r in rows if r[0] < 0][:MAX_ENTITY_LINES]
        rest = [r for r in rows if r[0] == 0]
        sensors = [r for r in rest if r[2] == 1][: MAX_ENTITY_LINES // 2]
        picked += sensors
        picked += [r for r in rest if r not in sensors][: max(0, MAX_ENTITY_LINES - len(picked))]
        picked = picked[:MAX_ENTITY_LINES]
        lines = [line for *_, line in sorted(picked, key=lambda r: (r[0], r[1], r[2], r[3]))]
        if len(rows) > MAX_ENTITY_LINES:
            lines.append(f"({len(rows) - MAX_ENTITY_LINES} more: use find_entities)")
        return lines

    def find_entities(self, query: str, area: str | None) -> str:
        words = [w for w in str(query or "").lower().replace("_", " ").replace(".", " ").split() if w]
        matches = []
        for row in self._candidates(area or None):
            entity_id, row_area, name, _tier, _state, device_class = row
            haystack = f"{entity_id} {name} {row_area} {device_class}".lower().replace("_", " ")
            if all(w in haystack for w in words):
                matches.append(self._line(*row))
        matches.sort()
        if not matches:
            areas = ", ".join(sorted(self.ha.registry.areas)) or "none"
            return f"No entity matches {query!r}. Try fewer or other English words. Areas: {areas}"
        more = f"\n({len(matches) - FIND_LIMIT} more: add a word)" if len(matches) > FIND_LIMIT else ""
        return "\n".join(matches[:FIND_LIMIT]) + more

    def _system(self, template: str = SYSTEM) -> str:
        services = self.safety.policy.get("allowed_services") or {}
        listed = "\n".join(f"  {d}: {', '.join(s)}" for d, s in sorted(services.items()))
        return template.replace("{yaml_rules}", YAML_RULES).replace("{services}", listed)

    async def _submit(self, args: dict, proposal: dict | None, trace: _Trace, **create) -> tuple[dict, list]:
        """One submit_automation call → (the proposal, its failed checks). The first call of a
        conversation creates the draft, later ones edit it."""
        text = str(args.get("yaml") or "")
        if proposal is None:
            proposal = await self.lifecycle.create(
                text,
                title=args.get("title"),
                rationale=args.get("rationale"),
                expected_triggers=args.get("expected_triggers"),
                run_id=trace.run_id,
                **create,
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
        return proposal, failed

    def _find(self, call: dict, trace: _Trace) -> dict:
        args = call["input"]
        found = self.find_entities(str(args.get("query") or ""), str(args.get("area") or "") or None)
        trace.step("tool", {"name": "find_entities", "arguments": args, "result": found[:2000]})
        return {"id": call["id"], "content": found}

    async def draft(self, request: str, *, task: str = "draft", **kwargs) -> dict:
        """Ask the model for an automation; return the proposal (draft or blocked). Never deploys.
        With `auto`, a local attempt that ends without a passing draft is retried once by the cloud
        (the expert); the blocked local proposal is then rejected in favour of the expert's."""
        try:
            proposal = await self._draft(request, task=task, **kwargs)
        except OrchestratorError as err:
            if err.code != "failed" or not self._local_tried or not self.can_ask_expert(task):
                raise
            kwargs.pop("new_run_id", None)
            return await self._draft(request, task=task, expert=True, **kwargs)
        if proposal["status"] == "blocked" and self._local_tried and self.can_ask_expert(task):
            kwargs.pop("new_run_id", None)
            try:
                better = await self._draft(request, task=task, expert=True, **kwargs)
            except OrchestratorError:
                return proposal
            if better["status"] == "draft":
                self.lifecycle.reject(proposal["id"], f"replaced by the expert's draft {better['id']}")
                return better
        return proposal

    async def _draft(
        self,
        request: str,
        *,
        room: str | None = None,
        origin_agent: str | None = None,
        run_id: str | None = None,
        ha_id: str | None = None,
        priority: Priority = Priority.INTERACTIVE,
        replaces_original: str | None = None,
        new_run_id: str | None = None,
        task: str = "draft",
        expert: bool = False,
    ) -> dict:
        """One drafting conversation. `new_run_id`: the id this draft's run gets (a caller that
        answers before the draft is done)."""
        self._local_tried = False
        if self.safety.kill_engaged:
            raise OrchestratorError("kill_switch", "the kill switch is engaged")
        request = request.strip()[:2000]
        lines = self.entity_lines(request, room)
        current = ""
        if ha_id:
            deployed = self.lifecycle.get_deployed(ha_id)
            current = "\n\nChange this deployed automation:\n" + deployed["versions"][0]["yaml"]
        areas = ", ".join(sorted(self.ha.registry.areas)) or "none"
        user = f"Request: {request}{current}\n\nAreas: {areas}\nEntities (id, name, area, tier, state):\n" + (
            "\n".join(lines) or "(none)"
        )
        transcript: list[dict] = [{"role": "user", "text": user}]
        who = "the cloud expert, " if expert else ""
        trace = _Trace(
            self,
            f"{who}{origin_agent or 'owner'} asks for an automation: {request}",
            "manual" if priority is Priority.INTERACTIVE else "poke",
            run_id=new_run_id,
            origin=(origin_agent, run_id),
        )
        trace.step("context", {"system": self._system(), "user": user, "entities": len(lines)})
        redactor = Redactor.for_home(self.ha)
        proposal = None
        searched, pushed_back = 0, False
        try:
            for _ in range(MAX_TURNS):
                turn = await self.turn(self._system(), transcript, TOOLS, priority, redactor, task, expert)
                self._local_tried = self._local_tried or turn.provider == "local"
                trace.llm(turn)
                if not turn.calls:
                    if proposal is not None:
                        break
                    transcript += [
                        {
                            "role": "assistant",
                            "text": turn.text or "…",
                            "raw": turn.raw,
                            "raw_by": turn.raw_by,
                        },
                        {"role": "user", "text": "Call submit_automation or decline."},
                    ]
                    continue
                results, done = [], False
                for call in turn.calls:
                    if not call.get("error") and call["name"] == "find_entities":
                        searched += 1
                        results.append(self._find(call, trace))
                        continue
                    if (
                        not call.get("error")
                        and call["name"] == "decline"
                        and not searched
                        and not pushed_back
                    ):
                        pushed_back = True  # small models give up on the first list; make them look once
                        results.append(
                            {
                                "id": call["id"],
                                "content": SEARCH_FIRST,
                                "is_error": True,
                            }
                        )
                        continue
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
                    proposal, failed = await self._submit(
                        call["input"],
                        proposal,
                        trace,
                        origin_agent=origin_agent,
                        ha_id=ha_id,
                        replaces_original=replaces_original,
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
                    {
                        "role": "assistant",
                        "text": turn.text,
                        "calls": turn.calls,
                        "raw": turn.raw,
                        "raw_by": turn.raw_by,
                    },
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

    # --- chat -------------------------------------------------------------------------------
    def house_summary(self) -> str:
        """What the orchestrator knows without asking: rooms, agents, open work. Local data only."""
        areas = ", ".join(sorted(self.ha.registry.areas)) or "none"
        lines = [f"Time: {datetime.now().astimezone():%Y-%m-%d %H:%M (%a)}", f"Areas: {areas}"]
        agents = self.agents_rt.agents.list() if self.agents_rt else []
        if agents:
            lines.append(
                "Room agents: "
                + ", ".join(
                    f"{a.id} ({a.room}, {self.agents_rt.effective_mode(a) if a.enabled else 'disabled'})"
                    for a in agents
                )
            )
        if self.council is not None:
            lines.append(f"House mode: {self.council.mode or 'unknown'}")
        lines.append(
            f"Proposals waiting for review: {self.lifecycle.count_open()}; "
            f"deployed automations: {self.lifecycle.count_deployed()}"
        )
        reports = self.store.query("SELECT agent_id, severity, summary FROM inbox ORDER BY id DESC LIMIT 5")
        if reports:
            lines.append("Latest agent reports:")
            lines += [f"- {r['agent_id']} [{r['severity']}]: {r['summary'][:160]}" for r in reports]
        return "\n".join(lines)

    async def chat(self, message: str, history: list[dict] | None = None) -> tuple[str, str]:
        """One chat turn with the owner → (reply, run id). Reads and drafts; never actuates."""
        if self.safety.kill_engaged:
            raise OrchestratorError("kill_switch", "the kill switch is engaged")
        earlier = "".join(
            f"{'Owner' if h['role'] == 'user' else 'You'}: {h['text']}\n"
            for h in (history or [])[-CHAT_HISTORY_TURNS:]
        )
        user = self.house_summary() + (f"\n\nEarlier in this chat:\n{earlier}" if earlier else "\n")
        user += f"\nOwner: {message.strip()[:4000]}"
        system = self._system(CHAT_SYSTEM)
        transcript: list[dict] = [{"role": "user", "text": user}]
        trace = _Trace(self, f"owner in chat: {message.strip()}", "chat")
        trace.step("context", {"system": system, "user": user})
        redactor = Redactor.for_home(self.ha)
        proposal, reply = None, ""
        try:
            for _ in range(MAX_TURNS):
                turn = await self.turn(system, transcript, CHAT_TOOLS, Priority.INTERACTIVE, redactor)
                trace.llm(turn)
                if not turn.calls:
                    reply = str(redactor.restore(turn.text)).strip()
                    break
                results = []
                for call in turn.calls:
                    if not call.get("error") and call["name"] == "find_entities":
                        results.append(self._find(call, trace))
                    elif not call.get("error") and call["name"] == "submit_automation":
                        proposal, failed = await self._submit(call["input"], proposal, trace)
                        results.append(
                            {
                                "id": call["id"],
                                "content": "Checks failed, fix and submit again:\n" + "\n".join(failed)
                                if failed
                                else f"Drafted as proposal {proposal['id']}; tell the owner to review it "
                                "on the Proposals page.",
                                "is_error": bool(failed),
                            }
                        )
                    else:
                        results.append(
                            {
                                "id": call["id"],
                                "content": f"unusable call: {call.get('error') or call['name']}",
                                "is_error": True,
                            }
                        )
                transcript += [
                    {
                        "role": "assistant",
                        "text": turn.text,
                        "calls": turn.calls,
                        "raw": turn.raw,
                        "raw_by": turn.raw_by,
                    },
                    {"role": "tool", "results": results},
                ]
        except OrchestratorError as err:
            trace.finish("error", error=str(err))
            raise
        except LifecycleError as err:
            trace.finish("error", error=str(err))
            raise OrchestratorError("failed", str(err)) from err
        if not reply:
            trace.finish("escalated", error="no answer within the turn limit")
            raise OrchestratorError("failed", "the model gave no answer within the turn limit")
        trace.step("final", {"text": reply})
        trace.finish("ok", summary=reply)
        return reply, trace.run_id

    async def summarize(self, data: str) -> tuple[str, str]:
        """The daily review's text for the owner: a `routine` task, local model only, no tools.
        → (text, run id). Raises OrchestratorError when the local model is unavailable."""
        trace = _Trace(self, "daily house review", "review")
        trace.step("context", {"system": REVIEW_SYSTEM, "user": data})
        try:
            turn = await self.turn(
                REVIEW_SYSTEM, [{"role": "user", "text": data}], [], Priority.BACKGROUND,
                Redactor.for_home(self.ha), "routine",
            )  # fmt: skip
        except OrchestratorError as err:
            trace.finish("error", error=str(err))
            raise
        trace.llm(turn)
        text = turn.text.strip()[:2000]
        trace.finish(
            "ok" if text else "escalated", summary=text[:300] or None, error=None if text else "empty"
        )
        return text, trace.run_id

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

        self.spawn(job(), f"draft-{agent_id}")
        return True

    def spawn(self, job, name: str) -> None:
        """A background job of the orchestrator; cancelled by stop()."""
        task = asyncio.get_running_loop().create_task(job, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class _Trace:
    """An orchestrator draft as a run (agent_id `orchestrator`), so Runs & traces shows it."""

    def __init__(self, orch: Orchestrator, reason: str, source: str, run_id=None, origin=None) -> None:
        self.store = orch.store
        self.run_id = run_id or new_id("r")
        self.started = time.time()
        self.trace: list[dict] = []
        self.steps = self.tokens_in = self.tokens_out = 0
        self.provider = None
        self.store.execute(
            "INSERT INTO runs (id, agent_id, status, wake_source, wake_reason, started, shadow, replay_of) "
            "VALUES (?, 'orchestrator', 'running', ?, ?, ?, 1, NULL)",
            (self.run_id, source, reason[:500], self.started),
        )
        if origin and origin[1]:
            self.step("origin", {"run_id": origin[1], "agent_id": origin[0]})

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
