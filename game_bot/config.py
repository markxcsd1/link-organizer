"""
Environment configuration and shared constants.

Only three variables are strictly required (Groq, Notion, and the API secret);
everything else degrades gracefully. With just Steam data the bot can still
resolve most games — IGDB, Groq, and Jina are enrichment/fallback layers.
"""
import os

# ── Required ──────────────────────────────────────────────────────────────────
GROQ_KEY   = os.environ["GROQ_API_KEY"]
NOTION_KEY = os.environ["NOTION_API_KEY"]
SECRET_KEY = os.environ["SECRET_KEY"]          # protects /api/logs-style endpoints

# ── Telegram (single authorised user) ────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID = os.environ.get("TELEGRAM_USER_ID", "")

# ── Notion ────────────────────────────────────────────────────────────────────
NOTION_DB_GAME = os.environ.get("NOTION_DB_GAME", "")   # the "To Play" database id
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# ── IGDB, via a free Twitch app (optional — enriches console platforms) ───────
TWITCH_CLIENT_ID     = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")

# ── Jina Reader (optional — a key raises the free rate limit) ────────────────
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")

# ── Groq models ──────────────────────────────────────────────────────────────
# Overridable via env so a Groq model deprecation is a config change, not a deploy.
# (Llama 3.1 8B / 3.3 70B were deprecated 2026-06-17; migrated to OpenAI GPT-OSS.)
GROQ_MODEL_FAST = os.environ.get("GROQ_MODEL_FAST", "openai/gpt-oss-20b")
GROQ_MODEL_CHAT = os.environ.get("GROQ_MODEL_CHAT", "openai/gpt-oss-120b")
