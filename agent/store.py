"""Per-session data store.

The base database (data/db.json) is loaded once. Every conversation session gets
its own deep copy, so test runs never affect each other. reset() restores it.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_BASE = None
_LOCK = threading.Lock()


def base_db() -> dict:
    global _BASE
    with _LOCK:
        if _BASE is None:
            _BASE = json.loads((ROOT / "data" / "db.json").read_text())
            _BASE["_auth"] = {a["customer_id"]: a for a in json.loads((ROOT / "data" / "auth_test_credentials.json").read_text())}
            _BASE["_kb"] = json.loads((ROOT / "data" / "kb" / "kb_articles.json").read_text())
    return _BASE


class Session:
    def __init__(self, customer_id: str | None = None, channel: str = "chat", guardrails: str = "strict", model: str | None = None,
                 session_id: str | None = None):
        # A harness may supply its own id (e.g. a per-scenario UUID) so later turns can find the session without
        # extracting anything from our responses. Only safe characters are kept.
        self.id = re.sub(r"[^\w.-]", "", session_id)[:64] if session_id else "S-" + uuid.uuid4().hex[:10]
        self.channel = channel
        self.guardrails = guardrails  # strict: tools enforce identity checks. off: the model alone must.
        self.model = model
        self.logged_in = customer_id
        self.reset()

    # ------------------------------------------------------------------ lifecycle
    def reset(self):
        self.db = copy.deepcopy({k: v for k, v in base_db().items() if not k.startswith("_")})
        self.auth = base_db()["_auth"]
        self.kb = base_db()["_kb"]
        self.sim_start = datetime.fromisoformat(self.db["meta"]["sim_now"])
        self.real_start = time.time()
        self.verified_pnrs: set[str] = set()
        self.verified_customers: set[str] = set()
        self.otp_verified: set[str] = set()
        self.failed_verifications = 0
        self.locked = False
        self.otps: dict[str, dict] = {}
        self.quotes: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}       # pending charges keyed by payment link id
        self.paylinks: dict[str, dict] = {}
        self.outbox: list[dict] = []             # SMS / email the system "sent"
        self.handoffs: list[dict] = []
        self.compensations: list[dict] = []
        self.messages: list[dict] = []           # LLM conversation (OpenAI format)
        self.trace: list[dict] = []              # per-turn records for testers
        self.ended = False
        self._index()

    def _index(self):
        db = self.db
        self.customers = {c["id"]: c for c in db["customers"]}
        self.flights = {f["id"]: f for f in db["flights"]}
        self.bookings = {b["pnr"]: b for b in db["bookings"]}
        self.routes = {r["id"]: r for r in db["routes"]}
        self.airports = {a["iata"]: a for a in db["airports"]}

    def now(self) -> datetime:
        return self.sim_start + timedelta(seconds=time.time() - self.real_start)

    # ------------------------------------------------------------------ lookups
    def segments(self, pnr):
        return sorted([s for s in self.db["booking_segments"] if s["pnr"] == pnr], key=lambda s: s["sequence"])

    def passengers(self, pnr):
        return [p for p in self.db["booking_passengers"] if p["pnr"] == pnr]

    def segpax(self, segment_id):
        return [sp for sp in self.db["segment_passengers"] if sp["segment_id"] == segment_id]

    def loyalty(self, cid):
        return next((l for l in self.db["loyalty_accounts"] if l["customer_id"] == cid), None)

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


SESSIONS: dict[str, Session] = {}


def create_session(**kw) -> Session:
    s = Session(**kw)
    SESSIONS[s.id] = s
    return s
