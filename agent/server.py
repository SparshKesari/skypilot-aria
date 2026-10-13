"""SkyPilot agent server. Standard library only.

    python agent/server.py            # http://localhost:8080

Endpoints (JSON):
  GET  /api/config                         users for "log in as", models, sim time
  POST /api/sessions                       {customer_id|null, model, guardrails, channel} -> session
  POST /api/sessions/{id}/messages         {text, model?} -> turn record + trace
  POST /api/sessions/{id}/reset            restore the original data for this session
  GET  /api/sessions/{id}/trace            full trace, outbox (SMS/email), handoffs
  GET  /api/sessions/{id}/db/{collection}  read-only data for testers (?pnr=, ?customer_id=)
  POST /api/sessions/{id}/tools/{name}     call one tool directly (dev / harness use)
  POST /api/pay/{session}/{link}           simulate the customer paying a payment link
  POST /converse                           one-shot helper for test harnesses
  POST /a2a                                TestMu AI Agent Testing contract: {input, session_id?} -> {output:[{role,content}], ...}
  POST /api/{model_id}/a2a                 Same contract with the model fixed by the URL (one endpoint per model for benchmarks)
  GET  /api/models                         Benchmark catalog: every model id, provider, readiness and its endpoint URL
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from llm import CONFIG, models  # noqa: E402
from loop import run_turn  # noqa: E402
from store import base_db, create_session  # noqa: E402
from session_store import STORE  # noqa: E402
from tools_impl import Tools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CHAT_DIR = ROOT / "channels" / "chat"
DEFAULT_GUARDRAILS = os.environ.get("GUARDRAILS", "strict")
API_KEY = os.environ.get("A2A_API_KEY")  # when set, every /api/*, /a2a and /converse request must carry it
PUBLIC_PATHS = ("/", "/index.html", "/brd", "/brd.html", "/benchmark", "/benchmark.html", "/api/config", "/api/models")


def resolve_model_id(mid):
    """Accept a model id or one of its aliases; return the canonical id or None."""
    for m in CONFIG["models"]:
        if mid == m["id"] or mid in (m.get("aliases") or []):
            return m["id"]
    return None


def pick_model(body):
    """Model comes from the JSON body; falls back to models.json "default". Unknown ids are a 400."""
    mid = body.get("model") or CONFIG["default"]
    canon = resolve_model_id(mid)
    if not canon:
        raise ValueError(f"unknown model {mid!r}; valid ids: {', '.join(m['id'] for m in CONFIG['models'])}")
    return canon


def model_catalog(host, scheme="http"):
    """One benchmark endpoint per model: POST {scheme}://{host}/api/<model_id>/a2a (TestMu contract)."""
    out = []
    for m in models():
        cfg = next(x for x in CONFIG["models"] if x["id"] == m["id"])
        out.append({**m, "aliases": cfg.get("aliases") or [], "endpoint": f"{scheme}://{host}/api/{m['id']}/a2a",
                    "note": cfg.get("note")})
    return out


def greeting(s):
    if s.logged_in:
        c = s.customers[s.logged_in]
        return f"Hi {c['first_name']}, I'm Aria, SkyPilot's virtual assistant. How can I help with your travel today?"
    return "Hi, I'm Aria, SkyPilot's virtual assistant. I can help you book, change or cancel flights, check flight status, and more. How can I help?"


def users():
    db = base_db()
    loy = {l["customer_id"]: l for l in db["loyalty_accounts"]}
    gt = json.loads((ROOT / "data" / "ground_truth.json").read_text())["scenarios"]
    scen = {}
    for g in gt:
        scen.setdefault(g["customer_id"], []).append({"scenario": g["scenario"], "message": g["sample_user_message"], "pnr": g.get("pnr"),
                                                    "last_name": g["last_name"], "otp": g["test_otp"]})
    nb = {}
    for b in db["bookings"]:
        nb[b["customer_id"]] = nb.get(b["customer_id"], 0) + 1
    return [{"id": c["id"], "name": f"{c['first_name']} {c['last_name']}", "persona": c["persona"].replace("_", " "), "tier": loy[c["id"]]["tier"],
             "home": c["home_airport"], "bookings": nb.get(c["id"], 0), "scenarios": scen.get(c["id"], [])} for c in db["customers"]]


class Handler(BaseHTTPRequestHandler):
    server_version = "SkyPilot/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("QUIET") != "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, obj, code=200):
        if self.command == "POST" and getattr(self, "_session", None) is not None:
            STORE.save(self._session)  # persist before the client sees the reply, so the next turn finds it
            self._session = None
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _file(self, path: Path, ctype):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "content-type")
        self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")
        self.end_headers()

    def session(self, sid):
        s = STORE.get(sid)
        if not s:
            raise KeyError("Unknown session")
        return self._track(s)

    def _track(self, s):
        """Remember the session this request touched so _json persists it before replying."""
        self._session = s
        return s

    def _authorized(self, path):
        if not API_KEY or path in PUBLIC_PATHS or path.startswith("/pay/"):
            return True
        given = self.headers.get("x-api-key") or (self.headers.get("authorization") or "").replace("Bearer", "").strip()
        return given == API_KEY

    def do_GET(self):
        self._session = None
        try:
            path = self.path.split("?")[0]
            if not self._authorized(path):
                return self._json({"error": "unauthorized: send x-api-key"}, 401)
            q = dict(p.split("=", 1) for p in self.path.split("?")[1].split("&")) if "?" in self.path else {}
            if path in ("/", "/index.html"):
                return self._file(CHAT_DIR / "index.html", "text/html; charset=utf-8")
            if path.startswith("/pay/"):
                return self._file(CHAT_DIR / "pay.html", "text/html; charset=utf-8")
            if path in ("/brd", "/brd.html"):
                return self._file(CHAT_DIR / "brd.html", "text/html; charset=utf-8")
            if path in ("/benchmark", "/benchmark.html"):
                return self._file(CHAT_DIR / "benchmark.html", "text/html; charset=utf-8")
            if path == "/api/models":
                return self._json({"default": CONFIG["default"], "sessions": STORE.name, "api_key_required": bool(API_KEY),
                                   "models": model_catalog(self.headers.get("host") or f"localhost:{os.environ.get('PORT', '8080')}",
                                                           self.headers.get("x-forwarded-proto") or "http")})
            if path == "/api/config":
                return self._json({"users": users(), "models": models(), "default_model": CONFIG["default"], "guardrails": DEFAULT_GUARDRAILS,
                                   "api_key_required": bool(API_KEY), "sim_now": base_db()["meta"]["sim_now"]})
            m = re.fullmatch(r"/api/sessions/([\w-]+)/trace", path)
            if m:
                s = self.session(m.group(1))
                return self._json({"session_id": s.id, "model": s.model, "guardrails": s.guardrails, "trace": s.trace, "outbox": s.outbox,
                                   "handoffs": s.handoffs, "compensations": s.compensations, "paylinks": list(s.paylinks.values())})
            m = re.fullmatch(r"/api/sessions/([\w-]+)/system_prompt", path)
            if m:
                from loop import system_prompt
                s = self.session(m.group(1))
                return self._json({"system_prompt": system_prompt(s), "tools": json.loads((ROOT / "tools" / "tools.json").read_text())["tools"]})
            m = re.fullmatch(r"/api/sessions/([\w-]+)/db/(\w+)", path)
            if m:
                s = self.session(m.group(1))
                rows = s.db.get(m.group(2))
                if not isinstance(rows, list):
                    return self._json({"error": "unknown collection"}, 404)
                for k, v in q.items():
                    rows = [r for r in rows if str(r.get(k)) == v]
                return self._json({"count": len(rows), "rows": rows[:200]})
            m = re.fullmatch(r"/api/paylinks/([\w-]+)/([\w-]+)", path)
            if m:
                s = self.session(m.group(1))
                return self._json(s.paylinks.get(m.group(2)) or {"error": "not found"})
            return self._json({"error": "not found"}, 404)
        except KeyError as e:
            return self._json({"error": str(e)}, 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


    def _a2a(self, body, forced_model):
        """TestMu AI Agent Testing contract.
        POST {input|message|text, session_id?|conversationId?, customer_id?, model?, guardrails?}
        -> {id, session_id, model, input:[{role:user,content}], output:[{role:assistant,content}], turn_record, trace, createdAt}
        forced_model (from /api/<model>/a2a) wins over body/header; a session stays on the model it was created with."""
        text_in = body.get("input") or body.get("message") or body.get("text") or body.get("query") or ""
        sid = body.get("session_id") or body.get("conversationId") or body.get("conversation_id") or self.headers.get("x-session-id")
        # A harness that fails to substitute its template sends the literal placeholder ("{{conversation_id}}"). Treat that as
        # no id: better a memory-less session than every scenario sharing one.
        if sid and (("{{" in str(sid)) or str(sid).strip("{} ").lower() in ("conversation_id", "session_id", "uuid", "none", "null")):
            sid = None
        model_req = forced_model or body.get("model") or self.headers.get("x-model")
        s = STORE.get(sid)
        if not s:
            s = create_session(customer_id=body.get("customer_id") or self.headers.get("x-customer-id") or None,
                               channel=body.get("channel", "chat"),
                               guardrails=body.get("guardrails") or self.headers.get("x-guardrails") or DEFAULT_GUARDRAILS,
                               model=pick_model({"model": model_req}), session_id=sid)
        elif model_req:
            s.model = pick_model({"model": model_req})
        self._track(s)
        if not text_in:
            return self._json({"id": s.id, "session_id": s.id, "model": s.model, "guardrails": s.guardrails, "input": [], "reply": greeting(s),
                               "output": [{"role": "assistant", "content": greeting(s)}], "createdAt": datetime.now(timezone.utc).isoformat()})
        if s.ended:
            reply = "This conversation was handed to a colleague. Start a new chat to continue."
            return self._json({"id": s.id, "session_id": s.id, "model": s.model, "input": [{"role": "user", "content": text_in}], "reply": reply,
                               "output": [{"role": "assistant", "content": reply}], "ended": True, "createdAt": datetime.now(timezone.utc).isoformat()})
        out = run_turn(s, text_in, s.model)
        # record what the harness sent (minus secrets) so session-continuity problems can be diagnosed from the trace
        out["trace"]["client"] = {"session_id_in_body": bool(body.get("session_id") or body.get("conversationId") or body.get("conversation_id")),
                                  "headers": {k: v for k, v in self.headers.items() if k.lower() not in ("authorization", "x-api-key", "cookie")},
                                  "body_keys": sorted(body.keys())}
        reply = out["turn"].get("reply") or ""
        return self._json({"id": s.id, "session_id": s.id, "model": s.model, "guardrails": s.guardrails,
                           "input": [{"role": "user", "content": text_in}], "reply": reply,
                           "output": [{"role": "assistant", "content": reply}],
                           "turn_record": out["turn"], "trace": out["trace"], "ended": s.ended,
                           "createdAt": datetime.now(timezone.utc).isoformat()})

    def do_POST(self):
        self._session = None
        try:
            path = self.path.split("?")[0]
            if not self._authorized(path):
                return self._json({"error": "unauthorized: send x-api-key"}, 401)
            body = self._body()
            if path == "/api/sessions":
                s = self._track(create_session(customer_id=body.get("customer_id") or None, channel=body.get("channel", "chat"),
                                                guardrails=body.get("guardrails") or DEFAULT_GUARDRAILS, model=pick_model(body)))
                return self._json({"session_id": s.id, "greeting": greeting(s), "model": s.model, "guardrails": s.guardrails, "logged_in": s.logged_in})
            m = re.fullmatch(r"/api/sessions/([\w-]+)/messages", path)
            if m:
                s = self.session(m.group(1))
                if body.get("model"):
                    s.model = pick_model(body)
                if s.ended:
                    return self._json({"turn": {"reply": "This conversation was handed to a colleague. Start a new chat to continue.", "intent": "handoff"}, "trace": None})
                return self._json(run_turn(s, body["text"], s.model))
            m = re.fullmatch(r"/api/sessions/([\w-]+)/reset", path)
            if m:
                s = self.session(m.group(1))
                s.reset()
                return self._json({"ok": True, "greeting": greeting(s)})
            m = re.fullmatch(r"/api/sessions/([\w-]+)/tools/(\w+)", path)
            if m:
                s = self.session(m.group(1))
                return self._json(Tools(s).call(m.group(2), body))
            m = re.fullmatch(r"/api/pay/([\w-]+)/([\w-]+)", path)
            if m:
                s = self.session(m.group(1))
                return self._json(Tools(s).pay_link(m.group(2)))
            m = re.fullmatch(r"/api/([\w.-]+)/(a2a|chat)", path)
            if m:
                # Per-model benchmark endpoint: the model is fixed by the URL, e.g. /api/claude-opus-5-5/a2a
                forced = resolve_model_id(m.group(1))
                if not forced:
                    return self._json({"error": f"unknown model {m.group(1)!r}; see GET /api/models"}, 404)
                return self._a2a(body, forced)
            if path in ("/a2a", "/a2a/chat"):
                return self._a2a(body, None)
            if path == "/converse":
                s = self._track(STORE.get(body.get("session_id")) or create_session(customer_id=body.get("customer_id"), channel=body.get("channel", "chat"),
                                                                                     guardrails=body.get("guardrails") or DEFAULT_GUARDRAILS, model=pick_model(body)))
                if body.get("model"):
                    s.model = pick_model(body)
                out = run_turn(s, body["text"], s.model)
                return self._json({"session_id": s.id, **out})
            return self._json({"error": "not found"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except KeyError as e:
            return self._json({"error": f"missing or unknown: {e}"}, 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)


def main():
    port = int(os.environ.get("PORT", "8080"))
    base_db()
    print(f"SkyPilot agent on http://localhost:{port}  (guardrails={DEFAULT_GUARDRAILS}, sessions={STORE.name}, api_key={'on' if API_KEY else 'off'})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
