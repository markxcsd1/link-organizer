"""
FastAPI application and Telegram interaction layer.

Flow: a message with a link → analyse it → post a confirmation card with a
"how hyped?" rating → the button tap saves the game to Notion. Pending cards are
held in an in-process dict, which is fine for a single-user bot.
"""
from __future__ import annotations
import re
import uuid
from fastapi import FastAPI, Request

from .config import TELEGRAM_USER_ID, NOTION_DB_GAME
from .text_utils import clean_field
from . import telegram, metadata, pipeline, notion, mapping, discovery, steam

app = FastAPI(title="Game Backlog Bot")


@app.middleware("http")
async def _restore_original_path(request: Request, call_next):
    """
    Vercel rewrites every /api/* request to the single /api/index function and
    (per vercel.json) carries the original sub-path as ?__p=... — the function
    otherwise only ever sees '/api/index'. Restore the real path so the router can
    match /api/telegram, /api/health, etc.
    """
    p = request.query_params.get("__p")
    if p is not None:
        real = "/api/" + p
        request.scope["path"] = real
        request.scope["raw_path"] = real.encode("utf-8")
    return await call_next(request)


# Pending confirmation cards, keyed by a short id embedded in the button callbacks.
PENDING: dict = {}

_VALID_STATUS = ("Unreleased", "Out", "Playing", "Finished")
_HYPE_BY_LEVEL = {"3": "★★★", "2": "★★", "1": "★"}

HELP_TEXT = (
    "🎮 *Game Backlog Bot*\n\n"
    "Send me a link to a game — a *trailer*, a *review*, or a *store page* — and I'll "
    "identify it, pull the developer, genres, platforms and release date, find its store "
    "page and a review, then ask how hyped you are before saving it to your *To Play* "
    "Notion database\\.\n\n"
    "Add a note by putting text after the link\\."
)


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/api/telegram")
async def telegram_webhook(req: Request):
    data = await req.json()

    if "callback_query" in data:
        await handle_callback(data["callback_query"])
        return {"ok": True}

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    user_id = str(message.get("from", {}).get("id", ""))
    text = message.get("text", "").strip()

    if user_id != TELEGRAM_USER_ID or not text or not chat_id:
        return {"ok": True}

    if text in ("/start", "/help"):
        await telegram.send(chat_id, HELP_TEXT)
    elif (m := re.search(r"https?://\S+", text)):
        url = m.group(0)
        note = text.replace(url, "").strip()
        await handle_game_link(chat_id, url, note)
    else:
        await telegram.send(chat_id, "Send me a game link (trailer, review, or store "
                                     "page) and I'll add it to your backlog. /help for details.")
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"ok": True, "v": "modular-1"}


# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_game_link(chat_id: int, url: str, note: str) -> None:
    """Analyse a shared link and present a confirmation card with hype buttons."""
    if not NOTION_DB_GAME:
        await telegram.send(chat_id, "⚠️ Game database not configured. Set NOTION_DB_GAME.")
        return

    meta = await metadata.fetch_page_meta(url)
    try:
        game = await pipeline.analyse_game_link(url, meta)
    except Exception as e:
        await telegram.send(chat_id, f"❌ Game analysis failed: {e}")
        return

    name = clean_field(game.get("name", "")) or meta.get("title", "") or url
    developer = clean_field(game.get("developer", ""))
    genres = mapping.map_genres(game.get("genres") or [])
    platforms = mapping.map_platforms(game.get("platforms") or [])
    status = game.get("status", "Out")
    if status not in _VALID_STATUS:
        status = "Out"
    release_date = clean_field(game.get("release_date", ""))     # exact ISO, else ""
    release_human = clean_field(game.get("release_human", ""))   # approx text, else ""
    store_url = game.get("store_url", "")
    summary = game.get("summary", "")
    if note:
        summary = note + (" — " + summary if summary else "")

    store_url, video_url, review_url = await _resolve_links(url, name, store_url)

    pid = str(uuid.uuid4())[:8]
    PENDING[pid] = {
        "chat_id": chat_id, "name": name[:200], "developer": developer,
        "genres": genres, "platforms": platforms, "status": status,
        "release_date": release_date, "store_url": store_url,
        "review_url": review_url, "video_url": video_url,
    }
    await telegram.send_buttons(
        chat_id,
        _card(name, developer, genres, platforms, status, release_date,
              release_human, store_url, video_url, review_url, summary),
        _hype_keyboard(pid),
    )


async def _resolve_links(url: str, name: str, store_url: str) -> tuple[str, str, str]:
    """Decide which link is the trailer vs. review, and auto-find whatever's missing."""
    if mapping.is_video_url(url):
        video_url, review_url = mapping.clean_video_url(url), ""
    elif mapping.is_game_url(url):
        store_url = store_url or url
        video_url = await discovery.find_trailer(name) if name else ""
        review_url = ""
    else:
        review_url = url
        video_url = await discovery.find_trailer(name) if name else ""
    video_url = mapping.clean_video_url(video_url) if video_url else ""

    if not store_url and name:
        store_url = await discovery.find_store_url(name)
    if not review_url and name:
        review_url = await discovery.find_review(name)
    # No critic review (e.g. an unreleased game) → link Steam's own reviews page,
    # which is guaranteed to be the right game because it uses the exact appid.
    if not review_url:
        appid = steam.appid_from_url(store_url)
        if appid:
            review_url = f"https://steamcommunity.com/app/{appid}/reviews/"
    return store_url, video_url, review_url


async def handle_callback(cq: dict) -> None:
    """Handle inline-button taps: hype:<pid>:<level>, save:<pid>, cancel:<pid>."""
    await telegram.answer_callback(cq["id"])
    if str(cq["from"]["id"]) != TELEGRAM_USER_ID:
        return

    data = cq.get("data", "")
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    action, _, rest = data.partition(":")

    if action == "cancel":
        PENDING.pop(rest, None)
        await telegram.edit_buttons(chat_id, message_id, cq["message"]["text"] + "\n\n_Cancelled._")
        return

    pid, _, level = rest.partition(":")
    pending = PENDING.pop(pid, None)
    if not pending:
        await telegram.edit_buttons(chat_id, message_id, "⏱ Action expired — send the link again.")
        return
    if action == "hype":
        pending["hype"] = _HYPE_BY_LEVEL.get(level, "★★")
    await telegram.edit_buttons(chat_id, message_id, cq["message"]["text"] + "\n\n_Saving…_")
    await _save(chat_id, pending, message_id)


async def _save(chat_id: int, pending: dict, message_id: int) -> None:
    try:
        notion_url = await notion.save_game(pending)
    except Exception as e:
        await telegram.edit_buttons(chat_id, message_id, f"❌ Failed to save: {e}")
        return
    reply = f"🎮 *Saved to To Play*\n*{pending['name']}*"
    if pending.get("developer"):
        reply += f"\n👨‍💻 {pending['developer']}"
    if pending.get("genres"):
        reply += "\n🏷️ " + "  ·  ".join(pending["genres"])
    reply += f"\n\n[Open in Notion]({notion_url})"
    await telegram.edit_buttons(chat_id, message_id, reply)


# ── Card rendering ────────────────────────────────────────────────────────────

def _card(name, developer, genres, platforms, status, release_date,
          release_human, store_url, video_url, review_url, summary) -> str:
    lines = [f"🎮 *{name}*"]
    if developer:
        lines.append(f"👨‍💻 {developer}")
    if genres:
        lines.append("🏷️ " + "  ·  ".join(genres))
    if platforms:
        lines.append("🖥️ " + "  ·  ".join(platforms))
    detail = status
    if release_date:
        detail += f"  ·  📅 {release_date}"
    elif release_human:
        detail += f"  ·  📅 ~{release_human}"
    lines.append(detail)
    if store_url:
        lines.append(f"🛒 [Store]({store_url})")
    if video_url:
        lines.append(f"🎬 [Trailer]({video_url})")
    if review_url:
        lines.append(f"📝 [Review]({review_url})")
    if summary:
        lines.append(f"\n{summary}")
    lines.append("\n*How hyped?*")
    return "\n".join(lines)


def _hype_keyboard(pid: str) -> list:
    return [
        [
            {"text": "🔥 ★★★", "callback_data": f"hype:{pid}:3"},
            {"text": "⭐ ★★",  "callback_data": f"hype:{pid}:2"},
            {"text": "🙂 ★",   "callback_data": f"hype:{pid}:1"},
        ],
        [
            {"text": "💾 Save (no hype)", "callback_data": f"save:{pid}"},
            {"text": "❌ Cancel",         "callback_data": f"cancel:{pid}"},
        ],
    ]
