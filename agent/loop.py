"""The agent loop: user message in, tool calls, one turn record out."""
from __future__ import annotations

import json
import re
import time

from assemble_prompt import build_system_prompt, load_tools, PROMPT_VERSION
from llm import chat, LLMError
from store import Session
from tools_impl import Tools

TOOLS = load_tools()
MAX_STEPS = 12
REQUIRED = ["reply", "intent", "skill", "verification", "actions_taken", "awaiting_confirmation", "policy_refs", "handoff", "injection_suspected"]


def session_label(s: Session) -> str:
    if not s.logged_in:
        return "anonymous (not logged in)"
    c = s.customers[s.logged_in]
    l = s.loyalty(c["id"])
    return f"logged in as {c['first_name']} {c['last_name']}, customer_id {c['id']}, SkyPoints {l['tier'] if l else 'BLUE'}"


def system_prompt(s: Session) -> str:
    caller = s.customers[s.logged_in]["phone"] if (s.channel == "voice" and s.logged_in) else "unknown"
    lang = s.customers[s.logged_in]["preferred_language"] if s.logged_in else "en"
    return build_system_prompt(s.channel, s.now().isoformat(timespec="minutes"), session_label(s), lang, caller)


def parse_turn(text: str | None) -> tuple[dict, str | None]:
    if not text:
        return {"reply": "", "intent": "unclear"}, "empty_response"
    raw = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    cand = m.group(1) if m else raw[raw.find("{"): raw.rfind("}") + 1] if "{" in raw else ""
    try:
        obj = json.loads(cand)
        missing = [k for k in REQUIRED if k not in obj]
        return obj, (f"missing_fields:{','.join(missing)}" if missing else None)
    except json.JSONDecodeError:
        return {"reply": raw, "intent": "unclear", "policy_refs": [], "handoff": False}, "not_json"


def run_turn(s: Session, user_text: str, model_id: str) -> dict:
    tools = Tools(s)
    if not s.messages:
        s.system = system_prompt(s)
    s.messages.append({"role": "user", "content": user_text})
    t0 = time.time()
    trace_start = len(s.trace)
    usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    steps, error, final, thinking_blocks, stop_reasons = 0, None, None, 0, []
    while steps < MAX_STEPS:
        steps += 1
        try:
            r = chat(model_id, s.system, s.messages, TOOLS)
        except LLMError as e:
            error = str(e)
            break
        for k in usage:
            usage[k] += r["usage"].get(k, 0) or 0
        thinking_blocks += r.get("thinking_blocks", 0)
        stop_reasons.append(r.get("stop_reason"))
        if r["tool_calls"]:
            s.messages.append({"role": "assistant", "content": r["content"], "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}} for tc in r["tool_calls"]],
                **({"_anthropic_content": r["raw_content"]} if r.get("raw_content") else {})})
            for tc in r["tool_calls"]:
                result = tools.call(tc["name"], tc["arguments"])
                s.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False)})
            continue
        s.messages.append({"role": "assistant", "content": r["content"], **({"_anthropic_content": r["raw_content"]} if r.get("raw_content") else {})})
        final = r["content"]
        break
    if error:
        turn, fmt = {"reply": "Sorry, I'm having trouble right now. Please try again in a moment.", "intent": "unclear"}, None
    elif final is None:
        turn, fmt = {"reply": "Sorry, that took too many steps. Let me hand you to a colleague.", "intent": "unclear"}, "max_steps"
    else:
        turn, fmt = parse_turn(final)
    calls = [t for t in s.trace[trace_start:] if t["type"] == "tool"]
    rec = {"type": "turn", "turn": sum(1 for t in s.trace if t.get("type") == "turn") + 1, "user": user_text, "model": model_id,
           "prompt_version": PROMPT_VERSION, "guardrails": s.guardrails, "channel": s.channel, "turn_record": turn,
           "format_error": fmt, "llm_error": error, "steps": steps, "tool_calls": [{"name": c["name"], "args": c["args"], "error": c["result"].get("error") if isinstance(c["result"], dict) else None} for c in calls],
           "tool_errors": sum(1 for c in calls if isinstance(c["result"], dict) and c["result"].get("error") in ("BAD_ARGUMENTS", "UNKNOWN_TOOL", "INTERNAL_ERROR")),
           "usage": usage, "thinking_blocks": thinking_blocks, "stop_reasons": stop_reasons, "latency_ms": int((time.time() - t0) * 1000), "ended": s.ended,
           "verification_state": {"pnrs": sorted(s.verified_pnrs), "customers": sorted(s.verified_customers), "locked": s.locked}}
    s.trace.append(rec)
    return {"turn": turn, "trace": rec, "tool_details": calls,
            "pending_links": [l for l in s.paylinks.values() if l["status"] == "unpaid"]}
