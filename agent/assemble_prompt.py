"""Build the full system prompt for one conversation.

Usage:
    python agent/assemble_prompt.py --channel chat --session anonymous > /tmp/prompt.md
    python agent/assemble_prompt.py --channel voice --caller-id +919811000210

The agent backend imports build_system_prompt() and calls it at the start of
each conversation. The prompt version (folder name) is recorded in every trace.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_VERSION = "v1"
P = ROOT / "prompts" / PROMPT_VERSION


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def build_system_prompt(channel: str, current_datetime: str, session: str = "anonymous",
                        language_hint: str = "en", caller_id: str = "n/a") -> str:
    if channel not in ("chat", "voice"):
        raise ValueError("channel must be chat or voice")
    skills = "\n\n".join(_read(f) for f in sorted((P / "skills").glob("*.md")))
    policies = "\n\n".join(_read(f) for f in sorted((P / "policies").glob("*.md")))
    values = {
        "current_datetime": current_datetime,
        "channel": channel,
        "language_hint": language_hint,
        "session": session,
        "caller_id": caller_id if channel == "voice" else "n/a",
        "skills": skills,
        "policies": policies,
        "channel_guidelines": _read(P / "channels" / f"{channel}.md"),
        "output_contract": _read(P / "output_contract.md"),
    }
    text = _read(P / "system.md")
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    if "{{" in text:
        raise RuntimeError("Unfilled placeholder in system prompt")
    return text


def load_tools() -> list:
    return json.loads((ROOT / "tools" / "tools.json").read_text())["tools"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="chat", choices=["chat", "voice"])
    ap.add_argument("--session", default="anonymous")
    ap.add_argument("--caller-id", default="n/a")
    ap.add_argument("--language", default="en")
    sim_now = json.loads((ROOT / "tools" / "fare_rules.json").read_text())["sim_now"]
    ap.add_argument("--now", default=sim_now)
    a = ap.parse_args()
    print(build_system_prompt(a.channel, a.now, a.session, a.language, a.caller_id))
