# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Principles

- **Minimize external services.** Solve problems with what's already in the stack before reaching for a new service.
- **Free only.** If an external service is unavoidable, it must have a free tier that covers this project's usage. Services currently in use: Vercel (free), Groq (free), Notion API (free).
- When adding a new dependency or service, explicitly note why it can't be avoided with existing tools.

## Project Overview

iOS Share Sheet → Vercel API → Groq (Llama 3.1 8B) classification → Notion database storage. Users share any link from their iPhone; the API classifies it and saves it to one of 6 Notion databases.

## Notion conventions

- The user keeps a top-level Notion page literally titled **"Bucket list"**. When the user asks to save something to a topic that doesn't exist yet (e.g. "save it to Tokyo"), the bot creates a new database with that topic name *under Bucket list* and saves there. Locate it via `notion_find_bucket_list()` — never hardcode the page id.
- Topic DBs (Bucket-list DBs and trip DBs like "Sifnos") share one schema, defined in `_TOPIC_DB_TYPE_OPTIONS` and `notion_create_topic_db()`. Inserts go through `insert_into_trip_db()`.
- Trip DBs (e.g. "⛵ Sifnos — Jul 17–20") may live under any parent page (the user has them under "☀️ Summer 2026"); they are NOT moved into Bucket list automatically.
- Working-state checkpoint: tag `v1-chat-works` at the commit where chat answers ferry/itinerary questions reliably.

## Game pipeline (To Play DB)

Game saves work backward from whatever the user shares — usually a **trailer (YouTube) or a review**, sometimes a store page:

1. `fetch_page_meta` reads the link (Jina-backed, see below) → raw title.
2. `igdb_search_game` resolves the canonical game and returns genre/developer/platform, the store URL (IGDB `websites`), and structured `release_dates`. **IGDB is NOT trusted for the release date** — its `first_release_date` back-fills a placeholder day for year/quarter/TBD entries. `_select_igdb_release` only yields an exact date for IGDB "exact" (category 0) entries.
3. The **store page** (located via IGDB `websites`, else a DuckDuckGo search in `_find_store_url`) is read through Jina and parsed by `_extract_store_release_date` — this is the authoritative date source.
4. Date precedence: **store exact > IGDB exact > approximate `human` text** (never fabricate a precise day; `Status` becomes Unreleased). Trailer/review/store are routed to the `Video`/`Review`/`Store` URL fields by input type; the missing one is auto-found (`find_game_trailer`).

The To Play DB has a `Store` URL property (added June 2026) alongside Review/Video.

## External services (why each is unavoidable)

Per the "minimize external services" principle, each non-core service earns its place:

- **Jina Reader** (`r.jina.ai`, free, optional `JINA_API_KEY`): the only way to get real titles + store release dates from JS-rendered pages (Xbox/Nintendo/PlayStation/Instagram/TikTok) that a plain `httpx` GET returns a shell for. Used as a *fallback* in `fetch_page_meta` — fast path is unchanged. A drop-in HTTP call, not a platform dependency.
- **IGDB** (free via Twitch): authoritative genre/developer/platform + store-URL resolution. Not used for dates.
- **Apify** (optional, free $5/mo, `APIFY_TOKEN`): Google Maps Scraper for accurate location ratings/reviews where DuckDuckGo scraping is unreliable. Strictly additive — `apify_maps_lookup` returns `{}` when the token is unset or the run is slow, falling back to `web_search`. **Google Places was rejected** (requires billing); Apify's free tier needs no card. Bounded to an 18s timeout; `vercel.json` raises `maxDuration` to 60s.

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
