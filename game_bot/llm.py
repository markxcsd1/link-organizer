"""Groq chat-completions client (OpenAI-compatible)."""
from __future__ import annotations
import httpx

from .config import GROQ_KEY, GROQ_MODEL_FAST

_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


async def complete(messages: list, max_tokens: int = 512,
                   model: str = GROQ_MODEL_FAST) -> str:
    """Return the assistant's message content for a chat completion."""
    payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
    # GPT-OSS are reasoning models — keep effort low so reasoning tokens don't eat
    # the answer budget (matters most for small, tightly-budgeted calls).
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "low"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(_ENDPOINT, json=payload, headers={
            "Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json",
        })
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()
