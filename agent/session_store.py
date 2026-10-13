"""Session persistence for stateless hosting (Vercel Functions).

Locally the server keeps sessions in process memory (MemoryStore). On Vercel every request may land on a
different instance, so each turn loads the session from Redis, runs, and saves it back (RedisStore).

A saved session is small: instead of the 7.5 MB database copy we store only the rows that differ from the
seed (changed rows by index + appended rows), plus the conversation, trace and verification state, as
zlib-compressed JSON. Typical size: tens of KB.

Backend selection (env):
  SESSION_STORE=memory|redis   explicit
  otherwise redis if UPSTASH_REDIS_REST_URL / KV_REST_API_URL is set, else memory.
Redis access is Upstash's REST protocol (Vercel Marketplace "Upstash Redis" / legacy "Vercel KV" env names),
standard library only.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import urllib.error
import urllib.request
import zlib
from datetime import datetime

from store import SESSIONS, Session, base_db

SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "21600"))  # 6 hours
_SET_FIELDS = ("verified_pnrs", "verified_customers", "otp_verified")
_PLAIN_FIELDS = ("channel", "guardrails", "model", "logged_in", "real_start", "failed_verifications", "locked",
                 "otps", "quotes", "pending", "paylinks", "outbox", "handoffs", "compensations", "messages", "trace", "ended")


# ---------------------------------------------------------------- (de)serialisation
def db_diff(db: dict) -> dict:
    """Rows that differ from the seed, per collection: {"changed": {index: row}, "appended": [rows]} or {"full": value}."""
    base = {k: v for k, v in base_db().items() if not k.startswith("_")}
    out = {}
    for name, rows in db.items():
        b = base.get(name)
        if not isinstance(rows, list) or not isinstance(b, list) or len(rows) < len(b):
            if rows != b:
                out[name] = {"full": rows}
            continue
        changed = {str(i): rows[i] for i in range(len(b)) if rows[i] != b[i]}
        appended = rows[len(b):]
        if changed or appended:
            out[name] = {"changed": changed, "appended": appended}
    return out


def db_apply(diff: dict) -> dict:
    db = copy.deepcopy({k: v for k, v in base_db().items() if not k.startswith("_")})
    for name, d in diff.items():
        if "full" in d:
            db[name] = d["full"]
            continue
        rows = db.setdefault(name, [])
        for i, row in d.get("changed", {}).items():
            rows[int(i)] = row
        rows.extend(d.get("appended", []))
    return db


def session_to_state(s: Session) -> dict:
    st = {"id": s.id, "sim_start": s.sim_start.isoformat(), "system": getattr(s, "system", None), "db": db_diff(s.db)}
    for f in _PLAIN_FIELDS:
        st[f] = getattr(s, f)
    for f in _SET_FIELDS:
        st[f] = sorted(getattr(s, f))
    return st


def session_from_state(st: dict) -> Session:
    s = Session.__new__(Session)
    s.id = st["id"]
    for f in _PLAIN_FIELDS:
        setattr(s, f, st[f])
    for f in _SET_FIELDS:
        setattr(s, f, set(st[f]))
    s.sim_start = datetime.fromisoformat(st["sim_start"])
    if st.get("system") is not None:
        s.system = st["system"]
    s.db = db_apply(st["db"])
    s.auth = base_db()["_auth"]
    s.kb = base_db()["_kb"]
    s._index()
    return s


def _to_json(x):
    """datetime -> {"__dt__": iso}; sets -> lists; anything else must already be JSON-safe (a function here is a bug)."""
    if isinstance(x, datetime):
        return {"__dt__": x.isoformat()}
    if isinstance(x, dict):
        return {str(k): _to_json(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_to_json(v) for v in x]
    if callable(x):
        raise TypeError("session state contains a function; it cannot be persisted")
    return x


def _from_json(x):
    if isinstance(x, dict):
        if set(x.keys()) == {"__dt__"}:
            return datetime.fromisoformat(x["__dt__"])
        return {k: _from_json(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_from_json(v) for v in x]
    return x


def encode(st: dict) -> str:
    return base64.b64encode(zlib.compress(json.dumps(_to_json(st), ensure_ascii=False).encode(), 6)).decode()


def decode(blob: str) -> dict:
    return _from_json(json.loads(zlib.decompress(base64.b64decode(blob)).decode()))


# ---------------------------------------------------------------- backends
class MemoryStore:
    name = "memory"

    def get(self, sid: str | None) -> Session | None:
        return SESSIONS.get(sid or "")

    def save(self, s: Session) -> None:
        SESSIONS[s.id] = s


class RedisStore:
    """Upstash REST: POST <url> with JSON command array, Authorization: Bearer <token>."""
    name = "redis"

    def __init__(self):
        self.url = (os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL") or "").rstrip("/")
        self.token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN") or ""
        if not self.url or not self.token:
            raise RuntimeError("Redis session store needs UPSTASH_REDIS_REST_URL/TOKEN (or KV_REST_API_URL/TOKEN)")

    def _cmd(self, *parts):
        req = urllib.request.Request(self.url, data=json.dumps(list(parts)).encode(),
                                     headers={"authorization": f"Bearer {self.token}", "content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read())
        if "error" in out:
            raise RuntimeError(f"redis: {out['error']}")
        return out.get("result")

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        blob = self._cmd("GET", f"skypilot:session:{sid}")
        return session_from_state(decode(blob)) if blob else None

    def save(self, s: Session) -> None:
        self._cmd("SET", f"skypilot:session:{s.id}", encode(session_to_state(s)), "EX", str(SESSION_TTL_SECONDS))
        SESSIONS.pop(s.id, None)  # never let the instance cache grow; Redis is the source of truth


class BlobStore:
    """Vercel Blob, standard library only. Public blobs sit behind a CDN with a 60 s minimum cache, so a session is never
    overwritten in place: every save writes a NEW versioned object (sessions/<sid>/<ms>-<n>.z), a load lists the prefix
    through the API (not cached) and fetches the newest object (a URL nobody has fetched before), then older versions are
    deleted. Paths contain the harness's session id (a UUID); the data is synthetic benchmark state."""
    name = "blob"
    API = "https://blob.vercel-storage.com"

    def __init__(self):
        self.token = os.environ.get("BLOB_READ_WRITE_TOKEN", "")
        if not self.token:
            raise RuntimeError("Blob session store needs BLOB_READ_WRITE_TOKEN")
        self.prefix = os.environ.get("BLOB_SESSION_PREFIX", "sessions")

    def _req(self, method, url, body=None, headers=None, raw=False):
        import time
        last = None
        for attempt in range(4):  # Blob occasionally answers 5xx; a lost session costs a whole scenario, so retry
            req = urllib.request.Request(url, data=body, method=method,
                                         headers={"authorization": f"Bearer {self.token}", "x-api-version": "7", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                    return data if raw else (json.loads(data) if data else {})
            except urllib.error.HTTPError as e:
                last = e
                if e.code < 500:
                    raise
            except urllib.error.URLError as e:
                last = e
            time.sleep(0.5 * (attempt + 1))
        raise last

    def _versions(self, sid):
        out = self._req("GET", f"{self.API}/?prefix={self.prefix}/{sid}/&limit=50")
        return sorted(out.get("blobs", []), key=lambda b: b["pathname"])

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        versions = self._versions(sid)
        if not versions:
            return None
        blob = self._req("GET", versions[-1]["url"], raw=True)
        sess = session_from_state(decode(blob.decode()))
        sess._blob_old = [b["url"] for b in versions]  # pruned on the next save without another listing
        return sess

    def save(self, s: Session) -> None:
        import time
        turns = sum(1 for t in s.trace if t.get("type") == "turn")
        path = f"{self.prefix}/{s.id}/{int(time.time() * 1000):013d}-{turns:04d}.z"
        self._req("PUT", f"{self.API}/{path}", body=encode(session_to_state(s)).encode(),
                  headers={"x-content-type": "application/octet-stream", "x-add-random-suffix": "0", "x-allow-overwrite": "1",
                           "x-cache-control-max-age": "60"})
        SESSIONS.pop(s.id, None)
        old = getattr(s, "_blob_old", None)
        try:  # prune the versions this request loaded; a failure here only costs storage
            if old:
                self._req("POST", f"{self.API}/delete", body=json.dumps({"urls": old}).encode(), headers={"content-type": "application/json"})
        except Exception:
            pass
        s._blob_old = []


def make_store():
    choice = os.environ.get("SESSION_STORE")
    if not choice:
        if os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL"):
            choice = "redis"
        elif os.environ.get("BLOB_READ_WRITE_TOKEN"):
            choice = "blob"
        else:
            choice = "memory"
    return {"redis": RedisStore, "blob": BlobStore}.get(choice, MemoryStore)()


STORE = make_store()
