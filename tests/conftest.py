"""Shared fixtures and env-var stubs for the test suite."""
import os
import pytest

# Stub every required env var BEFORE importing api.index
_ENV_STUBS = {
    "GROQ_API_KEY":        "test-groq-key",
    "NOTION_API_KEY":      "test-notion-key",
    "SECRET_KEY":          "test-secret",
    "TELEGRAM_BOT_TOKEN":  "test-tg-token",
    "TELEGRAM_USER_ID":    "123456",
    "NOTION_DB_LOCATION":  "loc-db-id",
    "NOTION_DB_PRODUCT":   "prod-db-id",
    "NOTION_DB_ARTICLE":   "art-db-id",
    "NOTION_DB_VIDEO":     "vid-db-id",
    "NOTION_DB_RECIPE":    "rec-db-id",
    "NOTION_DB_OTHER":     "oth-db-id",
}

for k, v in _ENV_STUBS.items():
    os.environ.setdefault(k, v)
