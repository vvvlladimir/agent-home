"""Redaction for cloud prompts: what leaves the house is a summary without personal data.

Replaced before a cloud call, restored in what comes back (only aliases are restored, never the
other placeholders):
- entity ids of people and their devices (`person.*`, `device_tracker.*`, `zone.*` except
  `zone.home`) → `person.person_1`, … (stable within one Redactor, restored in the answer);
- names the owner never wants to leave the house: the home's name, people's and zones' friendly
  names → `[name]`;
- e-mail addresses, URLs, IP and MAC addresses, phone numbers, coordinates, long secrets
  (tokens, keys, JWTs) → `[email]`, `[url]`, `[ip]`, `[mac]`, `[phone]`, `[coord]`, `[secret]`.

Deliberately simple and deny-leaning: a redacted word that was harmless costs a little model
quality, a leaked one cannot be taken back. Local calls are not redacted.
"""

from __future__ import annotations

import re

ALIASED_DOMAINS = ("person", "device_tracker", "zone")
PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[secret]"),  # JWT
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "[secret]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.I), "[url]"),
    (re.compile(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", re.I), "[mac]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "[ip]"),
    # IPv6: 4+ groups, or compressed with "::" (a time like 10:41:00 has neither)
    (re.compile(r"(?<![\w:])(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}(?![\w:])", re.I), "[ip]"),
    (re.compile(r"(?<![\w:])(?=[0-9a-f:]*::)[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?![\w:])", re.I), "[ip]"),
    (re.compile(r"(?<![\w.])-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}"), "[coord]"),  # 52.5200, 13.4050
    (re.compile(r"(?i)\b(latitude|longitude|lat|lon|lng)(\W{1,4})-?\d{1,3}\.\d+"), r"\1\2[coord]"),
    (re.compile(r"(?<![\w.])(?:\+|00)\d[\d ()/-]{6,}\d(?![\w.])"), "[phone]"),  # international
    (re.compile(r"(?<![\w.])0\d{2,4}[ /-]?\d{4,}(?![\w.])"), "[phone]"),  # national, 030 1234567
    (re.compile(r"\b[0-9a-f]{32,}\b", re.I), "[secret]"),  # hex keys
    # mixed-case keys/tokens; entity ids are lower case and never match
    (
        re.compile(r"(?<![\w/+])(?=[\w+/-]*[A-Z])(?=[\w+/-]*[a-z])(?=[\w+/-]*\d)[\w+/-]{24,}={0,2}"),
        "[secret]",
    ),
]
ENTITY = re.compile(r"\b([a-z][a-z0-9_]*)\.([a-z0-9_]+)\b")
GENERIC_WORDS = frozenset({"home", "house", "the", "and", "room", "work", "office", "zone", "phone"})


class Redactor:
    def __init__(self, names: list[str] | None = None) -> None:
        # longest first, so "Anna Maria" is replaced before "Anna"
        self.names = sorted(
            {n.strip() for n in names or [] if n and len(n.strip()) >= 3}, key=len, reverse=True
        )
        # name words as they appear inside entity ids (`light.anna_bedroom`): such ids are aliased
        self.tokens = {
            word
            for name in self.names
            for word in re.split(r"[^a-z0-9]+", name.lower())
            if len(word) >= 3 and word not in GENERIC_WORDS
        }
        self.aliases: dict[str, str] = {}  # real entity id → alias
        self._count: dict[str, int] = {}

    @classmethod
    def for_home(cls, ha) -> Redactor:
        """Names worth hiding in this home: its own name, people, zones."""
        names = [getattr(ha, "location_name", None) or ""]
        for entity_id, state in (getattr(ha, "states", None) or {}).items():
            if entity_id.split(".", 1)[0] in ALIASED_DOMAINS and entity_id != "zone.home":
                names.append(str((state.get("attributes") or {}).get("friendly_name") or ""))
        return cls([n for n in names if n and n.lower() not in ("home", "unknown")])

    def _alias(self, match: re.Match) -> str:
        domain, entity_id = match.group(1), match.group(0)
        if entity_id == "zone.home":
            return entity_id
        if domain not in ALIASED_DOMAINS and not self.tokens & set(match.group(2).split("_")):
            return entity_id
        if entity_id not in self.aliases:
            self._count[domain] = self._count.get(domain, 0) + 1
            self.aliases[entity_id] = f"{domain}.{domain}_{self._count[domain]}"
        return self.aliases[entity_id]

    def redact(self, text: str) -> str:
        if not text:
            return text
        text = ENTITY.sub(self._alias, text)
        for pattern, replacement in PATTERNS:
            text = pattern.sub(replacement, text)
        for name in self.names:
            text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "[name]", text, flags=re.I)
        return text

    def redact_value(self, value):
        """Redact every string inside a JSON-like value."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.redact_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.redact_value(v) for k, v in value.items()}
        return value

    def restore(self, value):
        """Put real entity ids back where the model used an alias (strings inside any JSON value)."""
        if isinstance(value, str):
            for real, alias in sorted(self.aliases.items(), key=lambda kv: len(kv[1]), reverse=True):
                value = re.sub(rf"\b{re.escape(alias)}\b", real, value)
            return value
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        if isinstance(value, dict):
            return {k: self.restore(v) for k, v in value.items()}
        return value
