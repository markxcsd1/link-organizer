"""Thin async wrappers over the Telegram Bot API (send / edit / acknowledge)."""
from __future__ import annotations
import httpx

from .config import TELEGRAM_TOKEN

_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


async def send(chat_id: int, text: str) -> None:
    if not TELEGRAM_TOKEN:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{_API}/sendMessage", json={
            "chat_id": chat_id, "text": text,
            "parse_mode": "Markdown", "disable_web_page_preview": True,
        })


async def send_buttons(chat_id: int, text: str, keyboard: list) -> None:
    if not TELEGRAM_TOKEN:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{_API}/sendMessage", json={
            "chat_id": chat_id, "text": text,
            "parse_mode": "Markdown", "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard},
        })


async def edit_buttons(chat_id: int, message_id: int, text: str,
                       keyboard: list | None = None) -> None:
    if not TELEGRAM_TOKEN:
        return
    payload: dict = {
        "chat_id": chat_id, "message_id": message_id, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{_API}/editMessageText", json=payload)


async def answer_callback(callback_id: str) -> None:
    """Acknowledge an inline-button tap so Telegram stops showing the spinner."""
    if not TELEGRAM_TOKEN:
        return
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(f"{_API}/answerCallbackQuery",
                          json={"callback_query_id": callback_id})
