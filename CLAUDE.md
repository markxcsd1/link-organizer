# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Principles

- **Minimize external services.** Solve problems with what's already in the stack before reaching for a new service.
- **Free only.** If an external service is unavoidable, it must have a free tier that covers this project's usage. Services currently in use: Vercel (free), Groq (free), Notion API (free).
- When adding a new dependency or service, explicitly note why it can't be avoided with existing tools.

## Project Overview

iOS Share Sheet → Vercel API → Groq (Llama 3.1 8B) classification → Notion database storage. Users share any link from their iPhone; the API classifies it and saves it to one of 6 Notion databases.

## Development Commands

```bash
# Run locally (requires .env to be populated)
uvicorn api.index:app --reload

# Test the classify endpoint
curl -X POST http://localhost:8000/api/classify \
  -H "Authorization: Bearer $SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'

# Health check
curl http://localhost:8000/api/health

# Deploy to Vercel (auto-deploys on push to main via GitHub integration)
vercel --prod
```

## Architecture

The entire backend is a single file: `api/index.py`. Vercel treats every file under `api/` as a serverless function; `vercel.json` rewrites all routes to this one handler.

**Request flow:**
1. `POST /api/classify` validates the `Authorization: Bearer <SECRET_KEY>` header
2. `classify()` sends the URL to Groq's OpenAI-compatible API (`llama-3.1-8b-instant`) and gets back `{category, name, notes}` as JSON
3. `save_to_notion()` creates a page in the matching Notion database using the Notion API v1
4. Returns `{ok, message, category, name, notion_url}` — the `message` field is displayed as the iOS notification

**Categories → Notion DB env var mapping:**
| Category | Env var |
|----------|---------|
| location | `NOTION_DB_LOCATION` |
| product | `NOTION_DB_PRODUCT` |
| article | `NOTION_DB_ARTICLE` |
| video | `NOTION_DB_VIDEO` |
| recipe | `NOTION_DB_RECIPE` |
| other | `NOTION_DB_OTHER` |

Unknown categories fall back to `other`.

## Environment Variables

All secrets are in `.env` locally and in Vercel project settings for production. See `.env.example` for the full list. The Notion integration must be manually shared with each of the 6 databases in Notion.

## Deployment

- **Vercel project:** `mike-x-projects/link-organizer`
- **Production URL:** `https://link-organizer-bay.vercel.app`
- GitHub repo (`markxcsd1/link-organizer`) is connected to Vercel — every push to `main` triggers an automatic deployment. Before doing that make the repo public and when done do it private again.
- Python runtime is inferred by Vercel from `requirements.txt` at the repo root.

## iOS Shortcut

The shortcut sends a POST request to `/api/classify` with `{"url": "<shared URL>"}` and displays the `message` field from the response as a notification. It appears under **Quick Actions** in the iOS 26 Share Sheet (not the main apps list).
