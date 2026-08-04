"""
Vercel entrypoint.

Vercel's Python runtime looks for a WSGI callable named `app`. The real
application lives at the repo root so that Flask resolves templates/ and
static/ relative to app.py, exactly as it does locally.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402  — path shim must run first

__all__ = ["app"]
