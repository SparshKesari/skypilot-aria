"""Model gateway for the Anthropic Messages API.

Internal message format is OpenAI chat-completions style; it is converted to Messages API blocks
(tool_use / tool_result) here. The roster in models.json holds the single model: Claude Opus 5.5.
Standard library only.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((Path(__file__).parent / "models.json").read_text())


def load_env():
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()


load_env()


class LLMError(Exception):
    pass


def models():
    out = []
    for m in CONFIG["models"]:
        ready = bool(os.environ.get(m["key_env"] or ""))
        out.append({"id": m["id"], "label": m["label"], "provider": m["provider"], "model": m["model"], "ready": ready})
    return out


def get_model(model_id):
    m = next((x for x in CONFIG["models"] if x["id"] == model_id), None)
    if not m:
        raise LLMError(f"Unknown model id {model_id}")
    return m


RETRY_STATUS = {429, 500, 502, 503, 504, 529}
RETRY_DELAYS = (2, 5, 10)  # seconds between attempts; transient provider errors must not count as agent failures


def _post(url, headers, body, timeout=180):
    data = json.dumps(body).encode()
    for attempt in range(len(RETRY_DELAYS) + 1):
        req = urllib.request.Request(url, data=data, headers={"content-type": "application/json", **headers}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode()[:500]
            # 429 with a daily/zero quota is not transient; only retry if the provider hints at a short wait.
            retryable = e.code in RETRY_STATUS and not (e.code == 429 and ("limit: 0" in text or "retry in" in text.lower() and "h" in text.lower().split("retry in")[-1][:12]))
            if retryable and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            raise LLMError(f"HTTP {e.code} from {url}: {text}" + (f" (after {attempt + 1} attempts)" if attempt else "")) from e
        except urllib.error.URLError as e:
            raise LLMError(f"Cannot reach {url}: {e.reason}") from e


def chat(model_id: str, system: str, messages: list, tools: list) -> dict:
    """Returns {"content": str|None, "tool_calls": [{"id","name","arguments"}], "usage": {...}, "latency_ms": int}."""
    m = get_model(model_id)
    t0 = time.time()
    if m["provider"] != "anthropic":
        raise LLMError(f"Unknown provider {m['provider']}")
    out = _anthropic(m, system, messages, tools)
    out["latency_ms"] = int((time.time() - t0) * 1000)
    out["provider"], out["model"] = m["provider"], m["model"]
    return out


# ---------------------------------------------------------------- anthropic
def _anthropic(m, system, messages, tools):
    key = os.environ.get(m["key_env"])
    if not key:
        raise LLMError(f"{m['key_env']} is not set")
    conv = []
    for msg in messages:
        if msg["role"] == "user":
            conv.append({"role": "user", "content": msg["content"]})
        elif msg["role"] == "assistant":
            if msg.get("_anthropic_content"):
                # Replay the exact blocks the model produced (incl. thinking blocks + signatures). Required for
                # same-model continuation on thinking models; other models drop foreign thinking blocks silently.
                conv.append({"role": "assistant", "content": msg["_anthropic_content"]})
                continue
            blocks = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": json.loads(tc["function"]["arguments"] or "{}")})
            conv.append({"role": "assistant", "content": blocks})
        elif msg["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": msg["content"]}
            if conv and conv[-1]["role"] == "user" and isinstance(conv[-1]["content"], list) and conv[-1]["content"] and conv[-1]["content"][0].get("type") == "tool_result":
                conv[-1]["content"].append(block)
            else:
                conv.append({"role": "user", "content": [block]})
    atools = [{"name": t["function"]["name"], "description": t["function"]["description"], "input_schema": t["function"]["parameters"]} for t in tools]
    if atools:
        atools[-1]["cache_control"] = {"type": "ephemeral"}
    body = {"model": m["model"], "max_tokens": CONFIG["max_tokens"],
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}], "messages": conv, "tools": atools}
    if m.get("sampling", True):  # Claude 4.7+ / 5.x reject temperature; thinking is always on there
        body["temperature"] = CONFIG["temperature"]
    base = os.environ.get("ANTHROPIC_API_BASE", "https://api.anthropic.com")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if os.environ.get("ANTHROPIC_WORKSPACE_ID"):  # needed when the key is org-level, not workspace-scoped
        headers["anthropic-workspace-id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
    r = _post(f"{base}/v1/messages", headers, body)
    text = "".join(b["text"] for b in r["content"] if b["type"] == "text") or None
    calls = [{"id": b["id"], "name": b["name"], "arguments": b["input"]} for b in r["content"] if b["type"] == "tool_use"]
    u = r.get("usage", {})
    return {"content": text, "tool_calls": calls, "usage": {"input_tokens": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                                                            "output_tokens": u.get("output_tokens", 0), "cached_tokens": u.get("cache_read_input_tokens", 0)},
            "stop_reason": r.get("stop_reason"), "raw_content": r["content"],
            "thinking_blocks": sum(1 for b in r["content"] if b["type"] in ("thinking", "redacted_thinking"))}
