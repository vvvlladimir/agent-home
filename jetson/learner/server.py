#!/usr/bin/env python3
"""The Jetson learner (D38): journal, nightly training, live suggestions, replay.

    GET  /healthz              no auth
    GET  /status               contracts/schemas/learner_status.schema.json
    PUT  /registry             contracts/schemas/house_registry.schema.json
    POST /events               contracts/schemas/house_events.schema.json → accepted + live suggestions
    GET  /model                contracts/schemas/house_model.schema.json (404 before the first training)
    GET  /now                  contracts/schemas/learner_now.schema.json
    POST /train                train now (202; 409 while a training runs)
    POST /replay               contracts/schemas/learner_replay.schema.json

API: contracts/learner.openapi.yaml. Every path but /healthz needs `Authorization: Bearer <token>`
(the Jetson's token file, shared with the metrics endpoint). It never talks to Home Assistant.

Runs from a checkout of the repository with the venv `jetson/setup.sh --learner` makes (Python ≥ 3.12,
agent_house/requirements.txt + numpy + scikit-learn): it imports core/predict.py and core/insights.py from
the add-on so both read the house the same way (like jetson/bench imports core/benchkit.py).

Environment: AH_ETC (/etc/agent-house: the token), AH_LEARNER_DATA (/var/lib/agent-house/learner: the
journal, the registry and the last model — put it on the NVMe), AH_LEARNER_HOST (0.0.0.0),
AH_LEARNER_PORT (9200), AH_TRAIN_AT (03:30, local), AH_RETENTION_DAYS (400).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
for path in (REPO / "agent_house", HERE.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import jsonschema  # noqa: E402

from learner import brain, replay  # noqa: E402
from learner.journal import Journal  # noqa: E402

VERSION = "0.1.0"
ETC = Path(os.environ.get("AH_ETC", "/etc/agent-house"))
DATA = Path(os.environ.get("AH_LEARNER_DATA", "/var/lib/agent-house/learner"))
SCHEMAS = REPO / "contracts" / "schemas"
MAX_BODY = 32 * 1024 * 1024
log = logging.getLogger("learner")


def read_token() -> str:
    try:
        return (ETC / "token").read_text().strip()
    except OSError:
        return ""


def _schema(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads((SCHEMAS / name).read_text()))


class Learner:
    """Everything the server does, without HTTP (tests drive it directly)."""

    def __init__(self, data_dir: Path, train_at: str = "03:30", retention_days: float = 400.0) -> None:
        self.journal = Journal(data_dir / "journal.db")
        self.model_file = data_dir / "model.json"
        self.registry_file = data_dir / "registry.json"
        self.train_at, self.retention_days = train_at, retention_days
        self.registry = self._load(self.registry_file)
        self.model = self._load(self.model_file)
        self.live: list[brain.LiveModel] = []
        self.lines: dict = {}
        self.lock = threading.Lock()  # live models and lines
        self.training = {"state": "never" if self.model is None else "ok", "started": None,
                         "finished": None, "duration_s": None, "error": None}  # fmt: skip
        self._train_thread: threading.Thread | None = None
        self.events_schema = _schema("house_events.schema.json")
        self.registry_schema = _schema("house_registry.schema.json")
        self._apply_time_zone()

    @staticmethod
    def _load(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _save(path: Path, doc: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        tmp.replace(path)

    def _apply_time_zone(self) -> None:
        """Hours and weekdays mean the house's (core/predict.py reads local time)."""
        tz = (self.registry or {}).get("time_zone")
        if isinstance(tz, str) and tz and os.environ.get("TZ") != tz and hasattr(time, "tzset"):
            os.environ["TZ"] = tz
            time.tzset()

    # --- input ------------------------------------------------------------------------------------
    def put_registry(self, doc: dict) -> dict:
        errors = sorted(self.registry_schema.iter_errors(doc), key=lambda e: list(e.path))
        if errors:
            raise ValueError(f"registry: {errors[0].message} at {list(errors[0].path)}")
        self.registry = doc
        self._save(self.registry_file, doc)
        self._apply_time_zone()
        return {"ok": True, "entities": len(doc["entities"])}

    def post_events(self, doc: dict, now: float | None = None) -> dict:
        errors = sorted(self.events_schema.iter_errors(doc), key=lambda e: list(e.path))
        if errors:
            raise ValueError(f"events: {errors[0].message} at {list(errors[0].path)}")
        events = doc["events"]
        accepted, duplicates = self.journal.add(events)
        if not doc.get("backfill"):
            with self.lock:
                for e in events:
                    if e["kind"] == "state" and e.get("state") is not None and e["entity_id"] in self.lines:
                        brain.append(self.lines[e["entity_id"]], float(e["ts"]), e["state"])
        touched = {e["entity_id"] for e in events}
        suggestions = [] if doc.get("backfill") else self.now(now, touched)["suggestions"]
        return {"ok": True, "accepted": accepted, "duplicates": duplicates,
                "last_ts": self.journal.stats()["last_ts"], "suggestions": suggestions}  # fmt: skip

    # --- output -----------------------------------------------------------------------------------
    def now(self, now: float | None = None, touched: set[str] | None = None) -> dict:
        """Suggestions worth asking or acting on (mode ≠ wait), for the models whose context changed."""
        t = now or time.time()
        out = []
        with self.lock:
            for m in self.live:
                if (
                    touched is not None
                    and m.entity_id not in touched
                    and not any(f[1] in touched for f in m.features if len(f) > 1)
                ):
                    continue
                try:
                    s = m.suggest(t, self.lines)
                except Exception:  # noqa: BLE001 - one broken model must not stop the others
                    log.exception("live model %s → %s failed", m.entity_id, m.state)
                    continue
                if s and s["mode"] != "wait":
                    out.append(s)
        return {"ts": t, "suggestions": out}

    def status(self) -> dict:
        return {
            "schema_version": 1,
            "version": VERSION,
            "journal": self.journal.stats(),
            "registry_ts": (self.registry or {}).get("ts"),
            "training": {**self.training, "next_at": self.next_training()},
            "model_ts": (self.model or {}).get("trained_at"),
            "live_targets": len(self.live),
        }

    # --- training ---------------------------------------------------------------------------------
    def next_training(self, now: float | None = None) -> float:
        hour, minute = (int(x) for x in self.train_at.split(":"))
        moment = datetime.fromtimestamp(now or time.time()).astimezone()
        due = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= moment:
            due += timedelta(days=1)
        return due.timestamp()

    def train(self, now: float | None = None) -> dict:
        """Synchronous; `start_training` runs it on a thread."""
        self.training.update(state="running", started=time.time(), error=None)
        try:
            self.journal.prune(self.retention_days, now)
            doc, live, lines = brain.train(self.journal, self.registry, now)
            with self.lock:
                self.live, self.lines, self.model = live, lines, doc
            self._save(self.model_file, doc)
            self.training.update(state="ok", finished=time.time(), duration_s=doc["duration_s"])
            log.info("trained in %.1f s: %d live targets, %d habits", doc["duration_s"], len(live),
                     len(doc["habits"]["predictions"]))  # fmt: skip
            return doc
        except Exception as err:
            self.training.update(state="failed", finished=time.time(), error=f"{type(err).__name__}: {err}")
            log.error("training failed:\n%s", traceback.format_exc())
            raise

    def start_training(self) -> bool:
        if self._train_thread and self._train_thread.is_alive():
            return False
        self._train_thread = threading.Thread(target=self._train_quietly, name="learner-train", daemon=True)
        self._train_thread.start()
        return True

    def _train_quietly(self) -> None:
        try:
            self.train()
        except Exception:  # noqa: BLE001 - recorded in self.training
            pass

    def schedule(self, stop: threading.Event) -> None:
        """Train once on start (the live models are not saved), then every night at `train_at`."""
        if self.journal.stats()["events"]:
            self.start_training()
        while not stop.is_set():
            due = self.next_training()
            if stop.wait(max(1.0, due - time.time())):
                return
            self.start_training()


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-house-learner/" + VERSION
    learner: Learner
    token: str = ""

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"ok": False, "error": message})

    def _authorized(self) -> bool:
        given = self.headers.get("Authorization", "")
        ok = bool(self.token) and hmac.compare_digest(given.encode(), f"Bearer {self.token}".encode())
        if not ok:
            self._error(401, "unauthorized" if self.token else "no token configured on the Jetson")
        return ok

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._error(413, "body too large")
            return None
        try:
            doc = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            self._error(400, "not JSON")
            return None
        if not isinstance(doc, dict):
            self._error(400, "expected a JSON object")
            return None
        return doc

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        if not self._authorized():
            return None
        if self.path == "/status":
            return self._send(200, self.learner.status())
        if self.path == "/model":
            model = self.learner.model
            return self._send(200, model) if model else self._error(404, "not trained yet")
        if self.path == "/now":
            return self._send(200, self.learner.now())
        return self._error(404, "not found")

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authorized():
            return None
        if self.path != "/registry":
            return self._error(404, "not found")
        doc = self._body()
        if doc is None:
            return None
        try:
            return self._send(200, self.learner.put_registry(doc))
        except ValueError as err:
            return self._error(400, str(err))

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return None
        if self.path == "/train":
            started = self.learner.start_training()
            return self._send(202, {"ok": True}) if started else self._error(409, "a training is running")
        if self.path not in ("/events", "/replay"):
            return self._error(404, "not found")
        doc = self._body()
        if doc is None:
            return None
        try:
            if self.path == "/events":
                return self._send(200, self.learner.post_events(doc))
            days = doc.get("days", 30)
            if not isinstance(days, int) or not 1 <= days <= 365:
                return self._error(400, "days must be 1…365")
            return self._send(200, replay.replay(self.learner.journal, doc.get("automation"), days))
        except ValueError as err:
            return self._error(400, str(err))

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        log.debug("%s %s", self.address_string(), format % args)


def serve(learner: Learner, host: str, port: int, token: str) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"learner": learner, "token": token})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    learner = Learner(
        DATA, os.environ.get("AH_TRAIN_AT", "03:30"), float(os.environ.get("AH_RETENTION_DAYS", "400"))
    )
    token = read_token()
    if not token:
        log.error("no token in %s/token: every request but /healthz is refused (run jetson/setup.sh)", ETC)
    stop = threading.Event()
    threading.Thread(target=learner.schedule, args=(stop,), name="learner-schedule", daemon=True).start()
    host = os.environ.get("AH_LEARNER_HOST", "0.0.0.0")
    port = int(os.environ.get("AH_LEARNER_PORT", "9200"))
    server = serve(learner, host, port, token)
    log.info("learner %s on %s:%d, journal %s", VERSION, host, port, learner.journal.path)
    try:
        server.serve_forever()
    finally:
        stop.set()
        learner.journal.close()


if __name__ == "__main__":
    main()
