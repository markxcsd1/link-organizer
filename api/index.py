"""
Vercel serverless entrypoint.

Vercel treats this file as the function and imports `app`. All logic lives in the
`game_bot` package at the repo root; this shim just puts the repo root on the
import path and re-exports the FastAPI app.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game_bot.bot import app  # noqa: E402

__all__ = ["app"]
