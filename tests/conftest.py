"""Stub the required environment variables before `game_bot` is imported."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_ENV = {
    "GROQ_API_KEY":         "test-groq-key",
    "NOTION_API_KEY":       "test-notion-key",
    "SECRET_KEY":           "test-secret",
    "TELEGRAM_BOT_TOKEN":   "test-tg-token",
    "TELEGRAM_USER_ID":     "123456",
    "NOTION_DB_GAME":       "game-db-id",
    "TWITCH_CLIENT_ID":     "test-twitch-id",
    "TWITCH_CLIENT_SECRET": "test-twitch-secret",
}
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)
