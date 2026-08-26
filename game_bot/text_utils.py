"""Small, pure string / JSON helpers (no I/O)."""
from __future__ import annotations
import json
import re


def rich_text(value: str) -> list:
    """Wrap a string as a Notion rich-text value (Notion caps each block at 2000 chars)."""
    return [{"text": {"content": value[:2000]}}]


def clean_field(val: str) -> str:
    """Drop LLM placeholder values like 'not found' / 'N/A' so they never reach Notion."""
    if not val:
        return ""
    if re.search(r"\b(not found|not specified|not available|unknown|n/a)\b", val, re.IGNORECASE):
        return ""
    return val


def extract_json(text: str) -> str:
    """Return the first complete top-level JSON object, matched by balanced braces."""
    start = text.find("{")
    if start == -1:
        return text
    depth, in_str, esc = 0, False, False
    for i, c in enumerate(text[start:], start):
        if esc:
            esc = False
            continue
        if c == "\\" and in_str:
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def strip_fences(text: str) -> str:
    """Strip ```json … ``` markdown fences an LLM may wrap its output in."""
    text = re.sub(r"^```[a-z]*\n?", "", text.strip(), flags=re.IGNORECASE)
    return re.sub(r"```$", "", text.strip())


def safe_json_loads(raw: str):
    """
    Best-effort parse of possibly-malformed LLM JSON. Tries the raw object, then a
    couple of cheap repairs (trailing commas, raw newlines in strings). Returns the
    parsed object, or None if unrecoverable — callers then retry or fall back.
    """
    candidate = extract_json(raw)
    for attempt in (
        candidate,
        re.sub(r",\s*([}\]])", r"\1", candidate),   # trailing commas before } or ]
        re.sub(r"(?<!\\)\n", " ", candidate),        # literal newlines inside strings
    ):
        try:
            return json.loads(attempt)
        except Exception:
            continue
    return None


def looks_like_game(text: str) -> bool:
    """Heuristic used only as a last resort when JSON parsing fails entirely."""
    return bool(re.search(
        r"\b(gameplay|launch trailer|reveal trailer|video ?game|early access|wishlist|"
        r"roguelite|roguelike|metroidvania|boss fight|nintendo switch|playstation|xbox|steam)\b",
        text or "", re.IGNORECASE))


def norm_name(s: str) -> str:
    """Lowercase alphanumerics only — for loose, punctuation-insensitive title matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())
