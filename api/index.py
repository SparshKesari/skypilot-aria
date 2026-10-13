"""Vercel Functions entry point. All paths are rewritten here (see vercel.json); the handler is the same
HTTP handler the local server uses, with sessions persisted in Redis instead of process memory."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
os.environ.setdefault("QUIET", "1")

from server import Handler  # noqa: E402


class handler(Handler):  # noqa: N801  (Vercel looks for a class named `handler`)
    pass
