from __future__ import annotations
import os, json, re, secrets, uuid, asyncio, httpx
from urllib.parse import unquote_plus
from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel

app = FastAPI()

GROQ_KEY         = os.environ["GROQ_API_KEY"]
NOTION_KEY       = os.environ["NOTION_API_KEY"]
SECRET_KEY       = os.environ["SECRET_KEY"]
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID = os.environ.get("TELEGRAM_USER_ID", "")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "markxcsd1/obsidian-vault")

NOTION_DB = {
    "location": os.environ["NOTION_DB_LOCATION"],
    "product":  os.environ["NOTION_DB_PRODUCT"],
    "article":  os.environ["NOTION_DB_ARTICLE"],
    "video":    os.environ["NOTION_DB_VIDEO"],
    "recipe":   os.environ["NOTION_DB_RECIPE"],
    "other":    os.environ["NOTION_DB_OTHER"],
}

NOTION_DB_LOGS        = os.environ.get("NOTION_DB_LOGS", "")
NOTION_DB_TRIP_PLACES = os.environ.get("NOTION_DB_TRIP_PLACES", "")
NOTION_DB_GAME        = os.environ.get("NOTION_DB_GAME", "")
TWITCH_CLIENT_ID      = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET  = os.environ.get("TWITCH_CLIENT_SECRET", "")

if NOTION_DB_GAME:
    NOTION_DB["game"] = NOTION_DB_GAME

CATEGORY_EMOJI = {
    "location": "📍",
    "product":  "🛍️",
    "article":  "📖",
    "video":    "🎬",
    "recipe":   "🍳",
    "game":     "🎮",
    "other":    "📌",
}

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# In-memory store for pending confirmations (works for single-user bot)
PENDING: dict = {}

# Conversation history per chat (keeps last 10 messages for context)
CHAT_HISTORY: dict = {}
MAX_HISTORY = 10

# Cached "Bucket list" page id for the lifetime of this serverless invocation
_BUCKET_LIST_ID: str | None = None

# IGDB OAuth token cache (valid ~60 days; re-fetched when expired)
_IGDB_TOKEN: str = ""
_IGDB_TOKEN_EXPIRY: float = 0.0

HELP_TEXT = """*Your personal knowledge assistant* 🧠

*Save a link:*
Just send any URL — I'll analyse it and ask before saving\\.
Add a note: `https://example\\.com great article`
Force category: `https://example\\.com !video`
After analysis, say *"save in my Sifnos list"* to add to a trip\\.

*Search your knowledge:*
`/search <query>` — search across all your Notion databases

*Recent saves:*
`/list` — show last 10 saves
`/list articles` — filter by category

*Topic lists:*
`/in <topic>` — list everything saved under that topic, e\\.g\\. `/in sifnos`
`/in <topic> <type>` — filter by type, e\\.g\\. `/in tokyo restaurant`
Or just ask: *"what restaurants do I have in Tokyo?"*

*Trip places database:*
`/setupdb` — create the Trip Places database in Notion \\(one\\-time setup\\)

*Create a note:*
`/note Meeting recap: we decided to\\.\\.\\.`

*Chat:*
Just talk to me in natural language — I'll search your knowledge base and answer\\."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rich_text(value: str) -> list:
    return [{"text": {"content": value[:2000]}}]

def _clean_field(val: str) -> str:
    """Strip Groq 'not found' placeholder values."""
    if not val:
        return ""
    if re.search(r'\b(not found|not specified|not available|unknown|n/a)\b', val, re.IGNORECASE):
        return ""
    return val

def _extract_json(text: str) -> str:
    """Extract the first complete JSON object by counting balanced braces."""
    start = text.find('{')
    if start == -1:
        return text
    depth, in_str, esc = 0, False, False
    for i, c in enumerate(text[start:], start):
        if esc:           esc = False; continue
        if c == '\\' and in_str: esc = True; continue
        if c == '"':      in_str = not in_str; continue
        if in_str:        continue
        if c == '{':      depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]

def parse_command(note: str) -> tuple:
    note = note.strip()
    if note.startswith("!"):
        parts = note.split(None, 1)
        cmd = parts[0][1:].lower()
        clean_note = parts[1].strip() if len(parts) > 1 else ""
        if cmd in NOTION_DB:
            return cmd, clean_note
    return None, note

async def tg_send(chat_id: int, text: str):
    if not TELEGRAM_TOKEN:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
        )

async def tg_send_buttons(chat_id: int, text: str, keyboard: list):
    if not TELEGRAM_TOKEN:
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

async def tg_edit_buttons(chat_id: int, message_id: int, text: str, keyboard: list | None = None):
    if not TELEGRAM_TOKEN:
        return
    payload: dict = {"chat_id": chat_id, "message_id": message_id,
                     "text": text, "parse_mode": "Markdown",
                     "disable_web_page_preview": True}
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText", json=payload)

async def tg_answer_callback(callback_id: str):
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id},
        )

async def groq_chat(messages: list, max_tokens: int = 512, model: str = "llama-3.1-8b-instant") -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": model, "max_tokens": max_tokens, "messages": messages},
        )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

async def fetch_page_meta(url: str) -> dict:
    """
    Generic metadata fetch — one request, follow all redirects, then:
    1. YouTube direct oEmbed (always reliable, handles Shorts)
    2. oEmbed auto-discovery for other platforms
    3. Open Graph tags
    4. Standard <title> / meta description
    5. Google Maps: extract place name from final URL
    """
    title, desc, author, final_url, html = "", "", "", url, ""

    # Fast path: YouTube direct oEmbed (page HTML is unreliable due to consent screens)
    if re.search(r'(youtube\.com|youtu\.be)', url, re.IGNORECASE):
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                oe = await client.get(f"https://www.youtube.com/oembed?url={url}&format=json")
            if oe.status_code == 200:
                od = oe.json()
                title  = od.get("title", "")[:200]
                author = od.get("author_name", "")
                if title:
                    # Try to grab the video description from page source
                    try:
                        async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}) as client:
                            rp = await client.get(url)
                        m = re.search(r'"shortDescription":"((?:[^"\\]|\\.){0,1500})"', rp.text)
                        if m:
                            desc = m.group(1).replace("\\n", "\n").replace('\\"', '"')[:800]
                    except Exception:
                        pass
                    return {"title": title, "desc": desc or f"YouTube video by {author}",
                            "author": author, "final_url": url, "maps_url": ""}
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(
            timeout=12, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        ) as client:
            r = await client.get(url)
        final_url = str(r.url)
        html = r.text
    except Exception:
        return {"title": "", "desc": "", "author": "", "final_url": url, "maps_url": ""}

    # 1. oEmbed auto-discovery — works for any platform that embeds a <link> tag
    oe_url = ""
    m = re.search(r'<link[^>]+type=["\']application/json\+oembed["\'][^>]+href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if not m:
        m = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/json\+oembed["\']', html, re.IGNORECASE)
    if m:
        oe_url = m.group(1)
    if oe_url:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                oe = await client.get(oe_url)
            if oe.status_code == 200:
                od = oe.json()
                title  = od.get("title", "")[:200]
                author = od.get("author_name", "")
                desc   = od.get("description", "")[:600] or (f"By {author}" if author else "")
        except Exception:
            pass

    # For YouTube: also grab video description from page source (richer than oEmbed)
    if "youtube" in final_url and not desc:
        m = re.search(r'"shortDescription":"((?:[^"\\]|\\.){0,1500})"', html)
        if m:
            desc = m.group(1).replace("\\n", "\n").replace('\\"', '"')[:800]

    # 2. Open Graph tags (fallback or supplement)
    def _og(prop: str) -> str:
        pat1 = rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)["\']'
        pat2 = rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:{prop}["\']'
        mm = re.search(pat1, html, re.IGNORECASE) or re.search(pat2, html, re.IGNORECASE)
        return mm.group(1).strip() if mm else ""

    if not title: title = _og("title")
    if not desc:  desc  = _og("description")

    # 3. Standard <title> and meta description
    if not title:
        m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        if m: title = m.group(1).strip()[:200]
    if not desc:
        for pat in [
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']',
        ]:
            m = re.search(pat, html, re.IGNORECASE)
            if m: desc = m.group(1).strip()[:400]; break

    # 4. Google Maps — extract place name from final URL; discard generic description
    maps_url = ""
    is_maps_url = re.search(
        r'(maps\.google\.|google\.[a-z.]+/maps|maps\.app\.goo\.gl|share\.google/)',
        url + " " + final_url, re.IGNORECASE)
    if is_maps_url:
        maps_url = final_url
        place = ""
        # Try to extract place from final URL path or query string
        pm = re.search(r'/maps/place/([^/@?&#]+)', final_url)
        if pm:
            place = unquote_plus(pm.group(1)).replace('+', ' ').strip()
        else:
            qm = re.search(r'[?&]q=([^&#]+)', final_url)
            if qm:
                place = unquote_plus(qm.group(1)).split(',')[0].strip()
        # If still no place (e.g. share.google JS redirect), try OG title from HTML
        if not place and title:
            GENERIC_TITLES = {"google maps", "maps", "google", "google search", ""}
            if title.lower().strip() not in GENERIC_TITLES:
                place = title  # OG title is already the place name
        if place:
            title = place[:200]
        desc = ""   # Google Maps' meta description is always useless

    # 5. Universal fallback: if title is still just the platform name, try noembed.com
    GENERIC = {"youtube", "google maps", "maps", "instagram", "facebook", "tiktok", "twitter", "x", ""}
    if title.lower().strip() in GENERIC:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                ne = await client.get(f"https://noembed.com/embed?url={url}")
            if ne.status_code == 200:
                nd = ne.json()
                if nd.get("title") and nd["title"].lower() not in GENERIC:
                    title  = nd.get("title", title)[:200]
                    author = nd.get("author_name", author)
                    desc   = nd.get("description", desc) or desc
        except Exception:
            pass

    return {
        "title": title[:200] if title else "",
        "desc":  desc[:600]  if desc  else "",
        "author": author,
        "final_url": final_url,
        "maps_url": maps_url,
    }


# ── Notion operations ─────────────────────────────────────────────────────────

async def web_search(query: str) -> str:
    """
    Search DuckDuckGo for review snippets.
    Tries Instant Answer first (structured data), then falls back to HTML search
    (gets real snippets from TripAdvisor, travel blogs, Google Maps listings, etc.)
    """
    parts = []

    # 1. Instant Answer API — structured data for well-known places
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"})
        if r.status_code == 200:
            d = r.json()
            abstract = d.get("AbstractText") or d.get("Answer") or ""
            if abstract:
                parts.append(abstract)
            for entry in d.get("Infobox", {}).get("content", []):
                lbl = entry.get("label", "").lower()
                val = entry.get("value", "")
                if val and lbl in ("rating", "address", "phone", "hours", "price range"):
                    parts.append(f"{lbl.title()}: {val}")
    except Exception:
        pass

    # 2. HTML search — gets real review snippets from the web
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                         "Accept-Language": "en-US,en;q=0.9"}) as client:
            r = await client.get("https://html.duckduckgo.com/html/",
                params={"q": f"{query} reviews"})
        if r.status_code == 200:
            raw_snippets = re.findall(
                r'class="result__snippet"[^>]*>(.*?)</(?:a|span)>', r.text, re.DOTALL)
            for s in raw_snippets[:5]:
                clean = re.sub(r'<[^>]+>', '', s)
                clean = re.sub(r'\s+', ' ', clean).strip()
                if clean and len(clean) > 25:
                    parts.append(clean)
    except Exception:
        pass

    return "\n".join(parts)[:1000]

async def notion_analyse_link(url: str, note: str, meta: dict) -> dict:
    """Deep analysis: extract the actual subject, do a web lookup for extra details."""
    title = meta.get("title", "")
    desc  = meta.get("desc", "")

    # Web lookup: always for Maps links; otherwise when content looks like a place
    web_info = ""
    is_maps = bool(meta.get("maps_url"))
    place_hint = re.search(
        r'(restaurant|bar|cafe|hotel|beach|tavern|bistro|shop|museum|place|spot)',
        title + " " + desc + " " + (note or ""), re.IGNORECASE)
    if is_maps or place_hint:
        name_after_pin = re.search(r'📍\s*(\S[^,\n]{1,50})', title)
        candidate = name_after_pin.group(1).strip() if name_after_pin else title[:60]

        # Build a rich search query with location context
        location_hint = ""
        maps_url = meta.get("maps_url", "")
        if maps_url:
            # For Maps links: pull location from URL query string
            qm = re.search(r'[?&]q=([^&#]+)', maps_url)
            if qm:
                full_q = unquote_plus(qm.group(1))
                parts_q = full_q.split(',')
                location_hint = ", ".join(parts_q[1:3]).strip() if len(parts_q) > 1 else ""
        else:
            # For other links (YouTube etc): extract location words from title/desc
            # Look for place names after the venue name
            loc_match = re.search(
                r'(?:in|at|,)\s+([A-Z][a-zA-Zα-ωΑ-Ω\s]{2,30}(?:,\s*[A-Z][a-zA-Zα-ωΑ-Ω\s]{2,20})?)',
                title + " " + desc)
            if loc_match:
                location_hint = loc_match.group(1).strip()[:60]

        # Build a richer search query: include type keywords from title too
        type_words = " ".join(re.findall(
            r'(restaurant|bar|cafe|hotel|beach bar|tavern|bistro|shop|museum)',
            title + " " + desc, re.IGNORECASE))
        search_q = " ".join(filter(None, [candidate, type_words[:30], location_hint])).strip()
        web_info = await web_search(search_q)

        # Pre-extract any rating found in the snippets so Groq doesn't miss it
        rating_match = re.search(
            r'rated?\s+([\d.]+)\s*(?:out of\s*[\d.]+|/\s*[\d.]+|stars?)?'
            r'|(\d+\.\d)\s*/\s*5'
            r'|\b([\d.]+)\s+(?:out of|/)\s*5',
            web_info, re.IGNORECASE)
        reviews_match = re.search(r'(\d[\d,]+)\s*(?:unbiased\s+)?reviews?', web_info, re.IGNORECASE)
        source_match  = re.search(r'(tripadvisor|google|yelp|booking|trustpilot)', web_info, re.IGNORECASE)
        if rating_match:
            rating_val = next(g for g in rating_match.groups() if g)
            rating_line = f"Rating: {rating_val}/5"
            if reviews_match:
                rating_line += f" ({reviews_match.group(1)} reviews)"
            if source_match:
                rating_line += f" — {source_match.group(1).title()}"
            web_info = rating_line + "\n" + web_info

    meta_text = ""
    if title:
        meta_text += f"Title: {title}\n"
    if desc:
        meta_text += f"Description: {desc[:600]}\n"
    if web_info:
        meta_text += f"Web info: {web_info}\n"

    prompt = (
        f"Analyse this link for a personal knowledge base. Focus on the SUBJECT, not the medium.\n"
        f"URL: {url}\n"
        f"{meta_text}"
        f"{'User note: ' + note if note else ''}\n\n"
        f"RULES:\n"
        f"1. Look past the URL type to the actual subject:\n"
        f"   • Video/reel about a restaurant → category=location, name=the restaurant name\n"
        f"   • Video about a recipe → category=recipe, name=the dish\n"
        f"   • Video reviewing a product → category=product, name=the product\n"
        f"   • Use category=video ONLY if the video itself is what matters (tutorial, documentary)\n"
        f"2. Extract the EXACT name. If the title contains '📍Name' or 'bar Name' or 'restaurant Name', use that exact name.\n"
        f"3. Extract the RATING from the web info if present (e.g. '4.5/5', '8.7/10', '4 stars'). Leave empty string if not found.\n"
        f"4. Summarise the REVIEW CONSENSUS using ONLY facts from the web info above. "
        f"If web info is empty or has no reviews, write a factual one-line description only — NEVER invent ratings, visitor quotes, or review summaries.\n"
        f"5. For ALL fields: if data is unavailable, use an empty string \"\". NEVER write 'not found', 'unknown', 'not specified', 'not available', 'N/A', or any similar phrase.\n"
        f"5. Generate a maps_link: Google Maps search URL for the place.\n\n"
        f"Categories: location, product, article, video, recipe, game, other\n"
        f"Use category=game for video game store pages, game trailers, or game-related links.\n\n"
        f"Return ONLY valid JSON, no markdown:\n"
        f'{{"category":"...","name":"exact venue/dish/product name","location":"neighbourhood, city, country","type":"e.g. Beach bar, Sushi restaurant, Boutique hotel","vibe":"2-3 adjectives","best_for":"e.g. sunset drinks, families, solo travelers","summary":"3-5 sentences: what makes it special, atmosphere, must-tries, and what reviews/visitors say about it","rating":"score if found in web info, else empty string","maps_link":"https://www.google.com/maps/search/Name+City","search_terms":["term1","term2"]}}'
    )
    raw = await groq_chat([{"role": "user", "content": prompt}], max_tokens=700)
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.IGNORECASE)
    raw = re.sub(r"```$", "", raw.strip())
    return json.loads(_extract_json(raw))

async def notion_save_page(db_id: str, properties: dict) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={"parent": {"database_id": db_id}, "properties": properties},
        )
    r.raise_for_status()
    return r.json()["url"]

async def notion_search(query: str) -> list:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.notion.com/v1/search",
            headers=NOTION_HEADERS,
            json={"query": query, "page_size": 10},
        )
    r.raise_for_status()
    results = []
    for obj in r.json().get("results", []):
        obj_type = obj.get("object", "page")
        title = "Untitled"
        url   = ""

        if obj_type == "database":
            # Database titles are at the top-level title array, NOT in properties
            title_arr = obj.get("title", [])
            if title_arr:
                title = title_arr[0].get("plain_text", "") or "Untitled"
        else:
            # Page titles are inside properties
            props = obj.get("properties", {})
            for val in props.values():
                if val.get("type") == "title":
                    items = val.get("title", [])
                    if items:
                        title = items[0].get("plain_text") or items[0].get("text", {}).get("content", "Untitled")
                        break
            for val in props.values():
                if val.get("type") == "url":
                    url = val.get("url") or ""
                    break

        results.append({"id": obj.get("id",""), "title": title, "url": url,
                         "notion_url": obj.get("url", ""), "object": obj_type})
    return results

async def notion_fetch_page_meta(page_id: str) -> dict:
    """Fetch a single page's (or database's) title and parent info."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS)
            # Notion returns 400 (not 404) when you call /v1/pages/{id} with a database id
            if r.status_code != 200:
                # Could be a database — try the databases endpoint
                r = await client.get(f"https://api.notion.com/v1/databases/{page_id}", headers=NOTION_HEADERS)
        if r.status_code != 200:
            return {}
        data = r.json()
        obj_type = data.get("object", "page")
        title = ""
        if obj_type == "database":
            title_arr = data.get("title", [])
            if title_arr:
                title = title_arr[0].get("plain_text", "")
        else:
            props = data.get("properties", {})
            for val in props.values():
                if val.get("type") == "title":
                    items = val.get("title", [])
                    if items:
                        title = items[0].get("plain_text", "")
                        break
        return {"id": data["id"], "title": title, "parent": data.get("parent", {}),
                "url": data.get("url", "")}
    except Exception:
        return {}

async def notion_query_db_rows(
    db_id: str,
    limit: int = 20,
    type_filter: str | None = None,
    location_contains: str | None = None,
) -> list:
    """Query rows of a topic/trip database, optionally filtered.

    - type_filter: matches Type select exactly (e.g. "Restaurant")
    - location_contains: substring match on Location rich_text
    """
    body: dict = {
        "sorts": [{"property": "Name", "direction": "ascending"}],
        "page_size": limit,
    }
    filters = []
    if type_filter:
        filters.append({"property": "Type", "select": {"equals": type_filter}})
    if location_contains:
        filters.append({"property": "Location", "rich_text": {"contains": location_contains}})
    if len(filters) == 1:
        body["filter"] = filters[0]
    elif len(filters) > 1:
        body["filter"] = {"and": filters}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=NOTION_HEADERS,
            json=body,
        )
    if r.status_code != 200:
        return []
    rows = []
    for page in r.json().get("results", []):
        props = page.get("properties", {})
        name    = (props.get("Name", {}).get("title") or [{}])[0].get("plain_text", "Untitled")
        type_s  = props.get("Type", {}).get("select") or {}
        loc_rt  = props.get("Location", {}).get("rich_text", [])
        rat_rt  = props.get("Rating", {}).get("rich_text", [])
        rows.append({
            "name":     name,
            "type":     type_s.get("name", ""),
            "location": loc_rt[0].get("plain_text", "") if loc_rt else "",
            "rating":   rat_rt[0].get("plain_text", "") if rat_rt else "",
            "notion_url": page.get("url", ""),
        })
    return rows

_TYPE_MAP = {
    "restaurant": "Restaurant", "tavern": "Restaurant", "taverna": "Restaurant",
    "bar": "Bar", "wine bar": "Bar", "beach bar": "Bar",
    "cafe": "Cafe", "coffee": "Cafe", "kafeneio": "Cafe",
    "beach": "Beach",
    "hotel": "Hotel", "hostel": "Hotel", "villa": "Hotel", "airbnb": "Hotel",
    "village": "Village", "town": "Village",
    "museum": "Museum", "gallery": "Museum",
    "shop": "Shop", "store": "Shop", "market": "Shop",
    "church": "Sight", "monastery": "Sight", "castle": "Sight", "ruins": "Sight",
    "sight": "Sight", "viewpoint": "Sight",
    # broader topic-DB types (also held by Bucket-list DBs)
    "place": "Place", "spot": "Place",
    "event": "Event", "concert": "Event", "show": "Event",
    "festival": "Festival",
    "activity": "Activity", "experience": "Activity", "tour": "Activity", "hike": "Activity",
}
_VALID_TYPES = {
    "Place","Restaurant","Bar","Cafe","Beach","Hotel","Village","Museum","Shop","Sight",
    "Event","Festival","Activity","Other",
}

def _map_type(raw: str) -> str:
    if not raw:
        return ""
    if raw in _VALID_TYPES:
        return raw
    lower = raw.lower()
    # Sort longest keys first so "village" is checked before "villa", etc.
    for k in sorted(_TYPE_MAP, key=len, reverse=True):
        if k in lower:
            return _TYPE_MAP[k]
    return "Other"

async def insert_into_trip_db(db_id: str, pending: dict) -> str:
    """Insert a place as a row into an island/trip database."""
    name     = pending.get("name", "")
    url      = pending.get("url", "") or "https://placeholder.com"
    maps     = pending.get("maps_link", "")
    type_raw = pending.get("type_", "")
    location = pending.get("location", "")
    rating   = pending.get("rating", "")
    notes    = pending.get("notes", "")
    vibe     = pending.get("vibe", "")
    best_for = pending.get("best_for", "")
    type_val = _map_type(type_raw)

    props: dict = {
        "Name": {"title": [{"text": {"content": name[:200]}}]},
        "URL":  {"url": url},
    }
    if maps:
        props["Maps"] = {"url": maps}
    if type_val:
        props["Type"] = {"select": {"name": type_val}}
    if location:
        props["Location"] = {"rich_text": _rich_text(location)}
    if rating:
        props["Rating"]   = {"rich_text": _rich_text(rating)}
    if notes:
        props["Notes"]    = {"rich_text": _rich_text(notes)}
    if vibe:
        props["Vibe"]     = {"rich_text": _rich_text(vibe)}
    if best_for:
        props["Best For"] = {"rich_text": _rich_text(best_for)}
    # Optional Date — only set if the analyzer/user supplied an ISO date string
    date_val = pending.get("date", "")
    if date_val:
        props["Date"] = {"date": {"start": date_val}}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={"parent": {"database_id": db_id}, "properties": props},
        )
    r.raise_for_status()
    return r.json()["url"]

async def notion_find_related(search_terms: list) -> list:
    """Find related Notion pages/databases. Databases (island DBs) are ranked first."""
    seen_ids    = set()
    seen_titles = set()
    databases   = []
    pages       = []
    for term in search_terms[:3]:
        try:
            results = await notion_search(term)
            for p in results[:6]:
                if not p["id"] or p["id"] in seen_ids or p["title"] in ("Untitled", ""):
                    continue
                norm_title = p["title"].lower().strip()
                if norm_title in seen_titles:
                    continue
                seen_ids.add(p["id"])
                seen_titles.add(norm_title)
                if p.get("object") == "database":
                    databases.append(p)
                else:
                    pages.append(p)
        except Exception:
            pass
    # Databases first (island DBs), then plain pages — cap at 4 total
    return (databases + pages)[:4]

async def notion_read_page_content(page_id: str, max_chars: int = 3000, _depth: int = 0) -> str:
    """Read the text content of a Notion page (its blocks), recursing into toggles and child pages."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=50",
                headers=NOTION_HEADERS)
        if r.status_code != 200:
            return ""
        lines = []
        for block in r.json().get("results", []):
            btype = block.get("type", "")
            bdata = block.get(btype, {})
            rt = bdata.get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rt)

            if btype in ("heading_1", "heading_2", "heading_3"):
                if text: lines.append(f"{'#' * int(btype[-1])} {text}")
            elif btype == "bulleted_list_item":
                if text: lines.append(f"• {text}")
            elif btype == "numbered_list_item":
                if text: lines.append(f"- {text}")
            elif btype == "to_do":
                if text: lines.append(f"{'✓' if bdata.get('checked') else '○'} {text}")
            elif btype == "callout":
                if text: lines.append(f"📌 {text}")
            elif btype == "child_page":
                # Inline sub-page: include its title as a heading then recurse (max 1 level deep)
                child_title = bdata.get("title", "")
                if child_title:
                    lines.append(f"## {child_title}")
                if _depth < 1:
                    sub = await notion_read_page_content(block["id"], max_chars=4000, _depth=_depth + 1)
                    if sub:
                        lines.append(sub)
            elif btype == "toggle":
                # Toggle block: show the label then recurse into hidden children
                if text: lines.append(f"▸ {text}")
                if _depth < 1 and block.get("has_children"):
                    sub = await notion_read_page_content(block["id"], max_chars=4000, _depth=_depth + 1)
                    if sub:
                        lines.append(sub)
            elif btype == "column_list":
                # Recurse into columns
                if _depth < 1 and block.get("has_children"):
                    sub = await notion_read_page_content(block["id"], max_chars=4000, _depth=_depth + 1)
                    if sub:
                        lines.append(sub)
            elif btype == "table":
                # Tables: rows are child blocks of type table_row
                if block.get("has_children"):
                    try:
                        async with httpx.AsyncClient(timeout=10) as tc:
                            tr = await tc.get(
                                f"https://api.notion.com/v1/blocks/{block['id']}/children?page_size=50",
                                headers=NOTION_HEADERS)
                        for row_block in tr.json().get("results", []):
                            if row_block.get("type") == "table_row":
                                cells = row_block["table_row"].get("cells", [])
                                row_text = " | ".join(
                                    "".join(t.get("plain_text", "") for t in cell)
                                    for cell in cells
                                )
                                if row_text.strip():
                                    lines.append(row_text)
                    except Exception:
                        pass
            else:
                if text: lines.append(text)

        return "\n".join(lines)[:max_chars]
    except Exception:
        return ""

async def notion_get_child_pages(page_id: str) -> list:
    """Return list of {id, title} for direct child pages (sub-pages) inside a Notion page."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=50",
                headers=NOTION_HEADERS)
        if r.status_code != 200:
            return []
        children = []
        for block in r.json().get("results", []):
            if block.get("type") == "child_page":
                children.append({
                    "id": block["id"],
                    "title": block.get("child_page", {}).get("title", "Untitled"),
                })
        return children
    except Exception:
        return []

async def notion_append_to_page(page_id: str, name: str, link_url: str, summary: str,
                                type_: str = "", location: str = "", rating: str = "") -> str:
    """Append a place entry to a Notion page using simple, reliable blocks."""
    # Build name text with optional hyperlink
    name_text: dict = {"content": name}
    if link_url:
        name_text["link"] = {"url": link_url}

    blocks = [{
        "object": "block", "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": name_text}]}
    }]

    # One detail line: type · location · rating — notes
    detail_parts = [p for p in [type_, location] if p]
    if rating:
        detail_parts.append(f"⭐ {rating}")
    detail = " · ".join(detail_parts)
    if summary:
        detail = (detail + " — " + summary) if detail else summary
    if detail:
        blocks.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": detail[:2000]},
                                         "annotations": {"color": "gray"}}]}
        })

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=NOTION_HEADERS,
            json={"children": blocks},
        )
    r.raise_for_status()
    return f"https://www.notion.so/{page_id.replace('-','')}"


# ── Trip Places database ──────────────────────────────────────────────────────

# Schema shared by topic DBs (bucket-list DBs and the legacy Trip Places DB)
_TOPIC_DB_TYPE_OPTIONS = [
    {"name": "Place",      "color": "default"},
    {"name": "Restaurant", "color": "orange"},
    {"name": "Bar",        "color": "purple"},
    {"name": "Cafe",       "color": "yellow"},
    {"name": "Beach",      "color": "blue"},
    {"name": "Hotel",      "color": "green"},
    {"name": "Village",    "color": "pink"},
    {"name": "Museum",     "color": "brown"},
    {"name": "Shop",       "color": "red"},
    {"name": "Sight",      "color": "default"},
    {"name": "Event",      "color": "gray"},
    {"name": "Festival",   "color": "pink"},
    {"name": "Activity",   "color": "blue"},
    {"name": "Other",      "color": "default"},
]


async def notion_create_topic_db(parent_page_id: str | None, title: str) -> str:
    """Create a topic-scoped database with the standard flexible schema.

    If parent_page_id is None, the DB is created at workspace root (used by /setupdb
    for the legacy Trip Places DB). Otherwise it's nested under that page — this is
    how bucket-list DBs (e.g. 'Tokyo', 'Japan trip') are created.
    """
    if parent_page_id:
        parent = {"type": "page_id", "page_id": parent_page_id}
    else:
        parent = {"type": "workspace", "workspace": True}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.notion.com/v1/databases",
            headers=NOTION_HEADERS,
            json={
                "parent": parent,
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": {
                    "Name":     {"title": {}},
                    "URL":      {"url": {}},
                    "Maps":     {"url": {}},
                    "Type":     {"select": {"options": _TOPIC_DB_TYPE_OPTIONS}},
                    "Location": {"rich_text": {}},
                    "Date":     {"date": {}},
                    "Rating":   {"rich_text": {}},
                    "Notes":    {"rich_text": {}},
                    "Vibe":     {"rich_text": {}},
                    "Best For": {"rich_text": {}},
                },
            },
        )
    r.raise_for_status()
    return r.json()["id"]


async def notion_create_trip_db() -> str:
    """Legacy: create the Trip Places DB at workspace root. Used by /setupdb."""
    return await notion_create_topic_db(None, "🗺️ Trip Places")


def _clean_topic_name(raw: str) -> str:
    """Normalize a user-supplied topic to a clean DB title.

    - Strips leading/trailing whitespace
    - Removes emoji and other non-letter/digit/space punctuation at the edges
    - Collapses multiple spaces
    - Title-cases multi-word names but preserves common ALL-CAPS acronyms (NYC, US, UK, etc.)
    """
    if not raw:
        return ""
    # Strip everything that isn't a letter or digit from the edges (kills emoji/punct)
    s = re.sub(r"^[^\w]+|[^\w]+$", "", raw, flags=re.UNICODE).strip()
    # Collapse internal whitespace
    s = re.sub(r"\s+", " ", s)
    if not s:
        return ""
    # Title-case, but keep short ALL-CAPS tokens as-is (e.g. NYC, LA, USA)
    parts = []
    for word in s.split(" "):
        if word.isupper() and 2 <= len(word) <= 4:
            parts.append(word)
        else:
            parts.append(word.capitalize())
    return " ".join(parts)


# Deterministic addto-intent regex — runs before the Groq classifier so that
# "Save it in Sifnos" is never misread as a generic save. The pattern intentionally
# requires BOTH a save/add verb AND a destination preposition + non-empty topic.
_ADDTO_RE = re.compile(
    r"""^\s*
        (?:save|add|put|store|drop|stash)\s+   # verb
        (?:it\s+|this\s+|that\s+)?             # optional pronoun
        (?:in|to|into|under|inside)\s+         # destination preposition
        (.+?)\s*$                              # the topic
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _detect_addto_intent(text: str) -> str | None:
    """Return the topic if `text` is an explicit 'save to <topic>' command, else None.

    Strips noise like 'my', 'the', 'our' prefix and ' list / page / database / db / trip'
    suffix so the topic comes out clean for Notion search.
    """
    if not text:
        return None
    m = _ADDTO_RE.match(text)
    if not m:
        return None
    topic = m.group(1).strip()
    if not topic:
        return None
    # Strip leading possessive/article
    topic = re.sub(r"^(?:my|the|our)\s+", "", topic, flags=re.IGNORECASE).strip()
    # Strip trailing collection-noun
    topic = re.sub(r"\s+(?:list|page|database|db|trip)\b.*$", "", topic, flags=re.IGNORECASE).strip()
    return topic or None


async def notion_find_bucket_list() -> str | None:
    """Find the user's 'Bucket list' page id. Caches the result per invocation."""
    global _BUCKET_LIST_ID
    if _BUCKET_LIST_ID:
        return _BUCKET_LIST_ID
    try:
        results = await notion_search("Bucket list")
        for r in results:
            if r.get("object") != "page":
                continue
            title = (r.get("title") or "").strip().lower()
            if title == "bucket list" or title.startswith("bucket list") or title.startswith("bucket"):
                _BUCKET_LIST_ID = r["id"]
                return _BUCKET_LIST_ID
    except Exception:
        pass
    return None


# ── Game DB helpers ───────────────────────────────────────────────────────────

_GAME_URL_RE = re.compile(
    r'(store\.steampowered\.com|gog\.com/game|epicgames\.com|itch\.io'
    r'|nintendo\.com/store|playstation\.com/[a-z-]+/games|xbox\.com/[a-z-]+/games)',
    re.IGNORECASE,
)

def _is_game_url(url: str) -> bool:
    return bool(_GAME_URL_RE.search(url))

_GAME_GENRE_MAP: dict[str, str] = {
    # Roguelite / Roguelike
    "roguelite": "Roguelite", "rogue-lite": "Roguelite",
    "roguelike": "Roguelike", "rogue-like": "Roguelike",
    # Deckbuilder
    "deckbuilder": "Deckbuilder", "deck builder": "Deckbuilder",
    "deck-builder": "Deckbuilder", "deckbuilding": "Deckbuilder",
    # Metroidvania
    "metroidvania": "Metroidvania",
    # Action (also covers IGDB "Hack and slash", "Shooter", "Beat 'em up")
    "action": "Action", "action-adventure": "Action", "action adventure": "Action",
    "hack and slash": "Action", "beat 'em up": "Action", "beat em up": "Action",
    "shooter": "Action",
    # Platformer (IGDB uses "Platform")
    "platformer": "Platformer", "platform": "Platformer",
    # Survivors-like
    "survivors-like": "Survivors-like", "survivors like": "Survivors-like",
    "survivor": "Survivors-like", "bullet heaven": "Survivors-like",
    # Strategy (IGDB uses "Real Time Strategy (RTS)", "Turn-based strategy (TBS)")
    "strategy": "Strategy", "turn-based": "Strategy", "rts": "Strategy",
    "real time strategy": "Strategy", "turn-based strategy": "Strategy",
    # RPG (IGDB uses "Role-playing (RPG)")
    "rpg": "RPG", "role-playing": "RPG", "role playing": "RPG", "role-playing (rpg)": "RPG",
}
_VALID_GAME_GENRES = frozenset({"Roguelite", "Roguelike", "Deckbuilder", "Metroidvania",
                                 "Action", "Platformer", "Survivors-like", "Strategy", "RPG"})

_GAME_PLATFORM_MAP: dict[str, str] = {
    # Longest keys first to avoid prefix conflicts (sorted at call time, but explicit order helps)
    "pc (microsoft windows)": "PC", "microsoft windows": "PC",
    "steam deck": "Steam Deck",
    "nintendo switch": "Switch",
    "playstation 5": "PS5", "ps5": "PS5",
    "xbox series x|s": "Xbox", "xbox series x": "Xbox", "xbox series s": "Xbox",
    "xbox series": "Xbox",
    "pc": "PC", "windows": "PC", "mac": "PC", "linux": "PC", "steam": "PC",
    "switch": "Switch", "nintendo": "Switch",
    "playstation": "PS5",
    "xbox": "Xbox",
}
_VALID_GAME_PLATFORMS = frozenset({"PC", "Switch", "PS5", "Xbox", "Steam Deck"})


def _map_game_genres(raw: list) -> list:
    """Map a list of raw genre strings to valid DB multi-select options."""
    result: list = []
    seen: set = set()
    for item in raw:
        s = str(item).strip()
        if s in _VALID_GAME_GENRES and s not in seen:
            result.append(s); seen.add(s); continue
        lower = s.lower()
        # longest-first so "rogue-lite" beats "rogue"
        for k in sorted(_GAME_GENRE_MAP, key=len, reverse=True):
            if k in lower:
                v = _GAME_GENRE_MAP[k]
                if v not in seen:
                    result.append(v); seen.add(v)
                break
    return result


def _map_game_platforms(raw: list) -> list:
    """Map a list of raw platform strings to valid DB multi-select options."""
    result: list = []
    seen: set = set()
    for item in raw:
        s = str(item).strip()
        if s in _VALID_GAME_PLATFORMS and s not in seen:
            result.append(s); seen.add(s); continue
        lower = s.lower()
        # longest-first so "steam deck" beats "steam"
        for k in sorted(_GAME_PLATFORM_MAP, key=len, reverse=True):
            if k in lower:
                v = _GAME_PLATFORM_MAP[k]
                if v not in seen:
                    result.append(v); seen.add(v)
                break
    return result


async def _get_igdb_token() -> str:
    """Return a cached IGDB/Twitch app-access token, refreshing when expired."""
    import time
    global _IGDB_TOKEN, _IGDB_TOKEN_EXPIRY
    if _IGDB_TOKEN and time.time() < _IGDB_TOKEN_EXPIRY - 60:
        return _IGDB_TOKEN
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id":     TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type":    "client_credentials",
            },
        )
    r.raise_for_status()
    data = r.json()
    _IGDB_TOKEN        = data["access_token"]
    _IGDB_TOKEN_EXPIRY = time.time() + data.get("expires_in", 3600)
    return _IGDB_TOKEN


async def igdb_search_game(name: str) -> dict:
    """Search IGDB for a game by name; return structured dict or {} on miss."""
    token = await _get_igdb_token()
    clean = re.sub(
        r'\s*[-|:]\s*(steam|on steam|buy|pc game|review|trailer|gameplay|official).*$',
        '', name, flags=re.IGNORECASE,
    ).strip()
    query = (
        f'search "{clean}"; '
        f'fields name,summary,genres.name,platforms.name,'
        f'involved_companies.company.name,involved_companies.developer,'
        f'first_release_date; '
        f'limit 1;'
    )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.igdb.com/v4/games",
            headers={
                "Client-ID":     TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {token}",
            },
            content=query,
        )
    r.raise_for_status()
    results = r.json()
    print(f"[igdb] query={clean!r} raw={results[:1]}")
    if not results:
        return {}
    game = results[0]

    # involved_companies can come back as ints (unexpanded) — guard with isinstance
    developer = ""
    for ic in game.get("involved_companies", []):
        if not isinstance(ic, dict):
            continue
        company = ic.get("company")
        if ic.get("developer") and isinstance(company, dict):
            developer = company.get("name", "")
            break
    # fallback: first company regardless of developer flag
    if not developer:
        for ic in game.get("involved_companies", []):
            if not isinstance(ic, dict):
                continue
            company = ic.get("company")
            if isinstance(company, dict) and company.get("name"):
                developer = company["name"]
                break

    genres    = _map_game_genres([
        g.get("name", "") for g in game.get("genres", []) if isinstance(g, dict)
    ])
    platforms = _map_game_platforms([
        p.get("name", "") for p in game.get("platforms", []) if isinstance(p, dict)
    ])

    release_date = ""
    ts = game.get("first_release_date")
    if ts:
        from datetime import datetime, timezone
        release_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    print(f"[igdb] parsed: developer={developer!r} release={release_date!r} "
          f"genres={genres} platforms={platforms}")
    return {
        "name":         game.get("name", clean),
        "developer":    developer,
        "genres":       genres,
        "platforms":    platforms,
        "release_date": release_date,
        "summary":      (game.get("summary") or "")[:500],
    }


async def analyse_game_link(url: str, meta: dict) -> dict:
    """Extract structured game data. Tries IGDB first; falls back to Groq."""
    from datetime import date as _date
    title = meta.get("title", "")
    desc  = meta.get("desc", "")

    # ── IGDB path ────────────────────────────────────────────────────────────
    if TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET and title:
        try:
            igdb = await igdb_search_game(title)
            print(f"[igdb] hit={bool(igdb.get('name'))} name={igdb.get('name','')!r} "
                  f"release={igdb.get('release_date','')!r} genres={igdb.get('genres')} "
                  f"platforms={igdb.get('platforms')}")
            if igdb.get("name"):
                # Derive status from release date
                status = "Out"
                if igdb.get("release_date"):
                    try:
                        rel = _date.fromisoformat(igdb["release_date"])
                        status = "Unreleased" if rel > _date.today() else "Out"
                    except ValueError:
                        pass
                igdb["status"] = status
                igdb.setdefault("summary", desc[:300] if desc else "")
                return igdb
        except Exception as e:
            print(f"[igdb] search failed: {e}")

    # ── Groq fallback ─────────────────────────────────────────────────────────
    prompt = (
        f"Extract game details from this URL and metadata. Use your knowledge of the game if you recognise it.\n"
        f"URL: {url}\n"
        f"Title: {title}\n"
        f"Description: {desc[:500]}\n\n"
        f"Return ONLY valid JSON — use empty string for unknown fields, never 'unknown' or 'N/A':\n"
        f'{{"name":"exact game name","developer":"studio name or empty",'
        f'"genres":["genre1"],"platforms":["platform1"],'
        f'"release_date":"YYYY-MM-DD or empty","status":"Unreleased or Out",'
        f'"summary":"1-2 sentences describing the game"}}\n\n'
        f"Genres — use only these exact values (pick all that apply): "
        f"Roguelite, Roguelike, Deckbuilder, Metroidvania, Action, Platformer, Survivors-like, Strategy, RPG\n"
        f"Platforms — use only these exact values: PC, Switch, PS5, Xbox, Steam Deck\n"
        f"Status: 'Unreleased' if not yet released, 'Out' if already out."
    )
    raw = await groq_chat([{"role": "user", "content": prompt}], max_tokens=400)
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.IGNORECASE)
    raw = re.sub(r"```$", "", raw.strip())
    result = json.loads(_extract_json(raw))
    # Normalise genres/platforms through the same mappers
    result["genres"]    = _map_game_genres(result.get("genres") or [])
    result["platforms"] = _map_game_platforms(result.get("platforms") or [])
    return result


_VIDEO_URL_RE = re.compile(
    r'(youtube\.com/watch|youtu\.be/|vimeo\.com/\d)', re.IGNORECASE
)

def _is_video_url(url: str) -> bool:
    return bool(_VIDEO_URL_RE.search(url))


async def find_game_trailer(game_name: str) -> str:
    """Search DuckDuckGo for a YouTube trailer. Returns the first YouTube URL found."""
    try:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                     "Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": f"{game_name} official trailer youtube"},
            )
        if r.status_code != 200:
            return ""
        # DuckDuckGo wraps external URLs as /l/?uddg=<percent-encoded url>
        from urllib.parse import unquote
        for m in re.finditer(r'uddg=(https?[^&"\']+)', r.text):
            decoded = unquote(m.group(1))
            if "youtube.com/watch" in decoded or "youtu.be/" in decoded:
                return decoded
    except Exception:
        pass
    return ""


async def save_game_to_notion(game: dict) -> str:
    """Insert a row into the 'To Play' game database."""
    props: dict = {
        "Name": {"title": [{"text": {"content": (game.get("name") or "")[:200]}}]},
    }
    if game.get("review_url"):
        props["Review"] = {"url": game["review_url"]}
    if game.get("video_url"):
        props["Video"] = {"url": game["video_url"]}
    if game.get("developer"):
        props["Developer"] = {"rich_text": _rich_text(game["developer"])}
    genres = game.get("genres") or []
    if genres:
        props["Genre"] = {"multi_select": [{"name": g} for g in genres]}
    platforms = game.get("platforms") or []
    if platforms:
        props["Platform"] = {"multi_select": [{"name": p} for p in platforms]}
    if game.get("status") in ("Unreleased", "Out", "Playing", "Finished"):
        props["Status"] = {"select": {"name": game["status"]}}
    if game.get("hype") in ("★★★", "★★", "★"):
        props["Hype"] = {"select": {"name": game["hype"]}}
    if game.get("release_date"):
        props["Release Date"] = {"date": {"start": game["release_date"]}}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={"parent": {"database_id": NOTION_DB_GAME}, "properties": props},
        )
    r.raise_for_status()
    return r.json()["url"]


async def handle_save_game_link(chat_id: int, url: str, note: str, meta: dict):
    """Analyse a game URL and present a confirmation card for the To Play DB."""
    if not NOTION_DB_GAME:
        await tg_send(chat_id,
            "⚠️ Game database not configured. Add NOTION_DB_GAME env var and redeploy.")
        return

    try:
        game_data = await analyse_game_link(url, meta)
    except Exception as e:
        await tg_send(chat_id, f"❌ Game analysis failed: {e}")
        return

    name      = _clean_field(game_data.get("name", "")) or meta.get("title", "") or url
    developer = _clean_field(game_data.get("developer", ""))
    genres    = _map_game_genres(game_data.get("genres") or [])
    platforms = _map_game_platforms(game_data.get("platforms") or [])
    status       = game_data.get("status", "Out")
    if status not in ("Unreleased", "Out", "Playing", "Finished"):
        status = "Out"
    release_date = _clean_field(game_data.get("release_date", ""))
    summary      = game_data.get("summary", "")
    if note:
        summary = note + (" — " + summary if summary else "")

    # Video: if the shared URL is a video use it directly; otherwise search for a trailer
    if _is_video_url(url):
        video_url  = url
        review_url = ""
    else:
        review_url = url
        video_url  = await find_game_trailer(name) if name else ""
    print(f"[game] video_url={video_url!r}")

    pid = str(uuid.uuid4())[:8]
    PENDING[pid] = {
        "action":       "save_link",
        "chat_id":      chat_id,
        "url":          url,
        "name":         name[:200],
        "category":     "game",
        "notes":        summary,
        "forced":       None,
        "ai_category":  "game",
        "related_pages": [],
        "developer":    developer,
        "genres":       genres,
        "platforms":    platforms,
        "status":        status,
        "release_date":  release_date,
        "review_url":    review_url,
        "video_url":     video_url,
    }

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
    if detail:
        lines.append(detail)
    if video_url:
        lines.append(f"🎬 [Trailer]({video_url})")
    if summary:
        lines.append(f"\n{summary}")
    lines.append("\n*How hyped?*")

    keyboard = [
        [
            {"text": "🔥 ★★★",  "callback_data": f"hype:{pid}:3"},
            {"text": "⭐ ★★",   "callback_data": f"hype:{pid}:2"},
            {"text": "🙂 ★",    "callback_data": f"hype:{pid}:1"},
        ],
        [
            {"text": "💾 Save (no hype)", "callback_data": f"save:{pid}"},
            {"text": "❌ Cancel",          "callback_data": f"cancel:{pid}"},
        ],
    ]
    await tg_send_buttons(chat_id, "\n".join(lines), keyboard)


async def _do_save_game(chat_id: int, pending: dict, message_id: int | None = None):
    """Persist a game pending entry to the To Play Notion DB."""
    try:
        notion_url = await save_game_to_notion({
            "name":       pending["name"],
            "review_url": pending.get("review_url", ""),
            "video_url":  pending.get("video_url", ""),
            "developer":  pending.get("developer", ""),
            "genres":     pending.get("genres", []),
            "platforms":  pending.get("platforms", []),
            "status":       pending.get("status", "Out"),
            "release_date": pending.get("release_date", ""),
            "hype":         pending.get("hype", ""),
        })
    except Exception as e:
        msg = f"❌ Failed to save: {e}"
        if message_id:
            await tg_edit_buttons(chat_id, message_id, msg)
        else:
            await tg_send(chat_id, msg)
        return

    reply = f"🎮 *Saved to To Play*\n*{pending['name']}*"
    if pending.get("developer"):
        reply += f"\n👨‍💻 {pending['developer']}"
    if pending.get("genres"):
        reply += "\n🏷️ " + "  ·  ".join(pending["genres"])
    reply += f"\n\n[Open in Notion]({notion_url})"

    if message_id:
        await tg_edit_buttons(chat_id, message_id, reply)
    else:
        await tg_send(chat_id, reply)


async def trip_places_add(pending: dict, trip_name: str) -> str:
    """Add a place to the Trip Places database."""
    if not NOTION_DB_TRIP_PLACES:
        raise ValueError("NOTION_DB_TRIP_PLACES not configured — run /setupdb first")
    name     = pending.get("name", "")
    url      = pending.get("url", "") or "https://placeholder.com"
    maps     = pending.get("maps_link", "") or "https://placeholder.com"
    notes    = pending.get("notes", "")
    type_    = pending.get("type_", "")
    location = pending.get("location", "")
    rating   = pending.get("rating", "")

    props: dict = {
        "Name":  {"title": [{"text": {"content": name[:200]}}]},
        "URL":   {"url": url},
        "Trip":  {"select": {"name": trip_name[:100]}},
    }
    if maps and maps != "https://placeholder.com":
        props["Maps"] = {"url": maps}
    if type_:
        props["Type"] = {"select": {"name": type_[:100]}}
    if location:
        props["Location"] = {"rich_text": _rich_text(location)}
    if rating:
        props["Rating"] = {"rich_text": _rich_text(rating)}
    if notes:
        props["Notes"] = {"rich_text": _rich_text(notes)}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={"parent": {"database_id": NOTION_DB_TRIP_PLACES}, "properties": props},
        )
    r.raise_for_status()
    return r.json()["url"]


async def trip_places_query(trip: str) -> list:
    """Query Trip Places database filtered by trip name."""
    if not NOTION_DB_TRIP_PLACES:
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_TRIP_PLACES}/query",
            headers=NOTION_HEADERS,
            json={
                "filter": {"property": "Trip", "select": {"equals": trip}},
                "sorts": [{"property": "Name", "direction": "ascending"}],
                "page_size": 20,
            },
        )
    if r.status_code != 200:
        return []
    results = []
    for page in r.json().get("results", []):
        props = page.get("properties", {})
        name  = (props.get("Name", {}).get("title") or [{}])[0].get("plain_text", "Untitled")
        type_ = props.get("Type", {}).get("select", {})
        type_name = type_.get("name", "") if type_ else ""
        loc_rt = props.get("Location", {}).get("rich_text", [])
        location = loc_rt[0].get("plain_text", "") if loc_rt else ""
        rat_rt = props.get("Rating", {}).get("rich_text", [])
        rating = rat_rt[0].get("plain_text", "") if rat_rt else ""
        results.append({"name": name, "type": type_name, "location": location,
                        "rating": rating, "notion_url": page.get("url", "")})
    return results

async def notion_create_standalone_page(title: str, content: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={
                "parent": {"type": "workspace", "workspace": True},
                "properties": {"title": {"title": [{"text": {"content": title}}]}},
                "children": [{"object":"block","type":"paragraph",
                               "paragraph":{"rich_text":[{"type":"text","text":{"content":content}}]}}],
            },
        )
    r.raise_for_status()
    return r.json()["url"]

async def notion_list_recent(category: str | None = None, limit: int = 10) -> list:
    db_ids = [NOTION_DB[category]] if category and category in NOTION_DB else list(NOTION_DB.values())
    pages = []
    async with httpx.AsyncClient(timeout=15) as client:
        for db_id in db_ids:
            r = await client.post(
                f"https://api.notion.com/v1/databases/{db_id}/query",
                headers=NOTION_HEADERS,
                json={"sorts": [{"timestamp": "created_time", "direction": "descending"}],
                      "page_size": limit},
            )
            if r.status_code == 200:
                for page in r.json().get("results", []):
                    props = page.get("properties", {})
                    title = "Untitled"
                    url = ""
                    for val in props.values():
                        if val.get("type") == "title":
                            items = val.get("title", [])
                            if items:
                                title = items[0].get("plain_text") or items[0].get("text", {}).get("content", "Untitled")
                                break
                    for val in props.values():
                        if val.get("type") == "url":
                            url = val.get("url") or ""
                            break
                    cat = next((k for k, v in NOTION_DB.items() if v == db_id), "other")
                    pages.append({
                        "title": title, "url": url,
                        "notion_url": page["url"],
                        "category": cat,
                        "time": page["created_time"][:10],
                    })
    pages.sort(key=lambda x: x["time"], reverse=True)
    return pages[:limit]

async def save_log(url: str, note: str, forced: str | None, ai_category: str,
                   final_category: str, name: str, status: str):
    if not NOTION_DB_LOGS:
        return
    try:
        properties = {
            "Name":            {"title": [{"text": {"content": (name or url)[:200]}}]},
            "URL":             {"url": url or "https://placeholder.com"},
            "Note":            {"rich_text": _rich_text(note)},
            "Forced Category": {"rich_text": _rich_text(forced or "")},
            "AI Category":     {"rich_text": _rich_text(ai_category)},
            "Final Category":  {"rich_text": _rich_text(final_category)},
            "Status":          {"rich_text": _rich_text(status)},
        }
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post("https://api.notion.com/v1/pages",
                              headers=NOTION_HEADERS,
                              json={"parent": {"database_id": NOTION_DB_LOGS},
                                    "properties": properties})
    except Exception:
        pass


# ── GitHub / Obsidian operations ─────────────────────────────────────────────

def _gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

def _sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()[:100] or "Untitled"

async def obsidian_create_note(title: str, content: str, folder: str = "Notes") -> str:
    import base64
    filename = f"{folder}/{_sanitize(title)}.md"
    body = f"# {title}\n\n{content}\n"
    encoded = base64.b64encode(body.encode()).decode()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}",
            headers=_gh_headers(),
            json={"message": f"Add note: {title}", "content": encoded},
        )
    r.raise_for_status()
    return f"https://github.com/{GITHUB_REPO}/blob/main/{filename}"

async def obsidian_search(query: str) -> list:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://api.github.com/search/code?q={query}+repo:{GITHUB_REPO}",
            headers=_gh_headers(),
        )
    if r.status_code != 200:
        return []
    results = []
    for item in r.json().get("items", [])[:6]:
        results.append({
            "title": item["name"].replace(".md", ""),
            "path": item["path"],
            "url": item["html_url"],
        })
    return results

async def obsidian_list_recent(limit: int = 8) -> list:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page={limit}",
            headers=_gh_headers(),
        )
    if r.status_code != 200:
        return []
    results = []
    seen = set()
    for commit in r.json():
        msg = commit["commit"]["message"]
        url = commit["html_url"]
        if msg not in seen:
            seen.add(msg)
            results.append({"title": msg, "url": url})
    return results


# ── Bot handlers ──────────────────────────────────────────────────────────────

async def handle_save_link(chat_id: int, url: str, note: str):
    """Analyse link and ask for confirmation before saving."""
    forced_category, clean_note = parse_command(note)
    await tg_send(chat_id, "🔍 Analysing…")

    meta = await fetch_page_meta(url)

    # Fast path: known game store URL or !game force → game flow
    if _is_game_url(url) or forced_category == "game":
        await handle_save_game_link(chat_id, url, clean_note, meta)
        return

    try:
        result = await notion_analyse_link(url, clean_note, meta)
    except Exception as e:
        await tg_send(chat_id, f"❌ Analysis failed: {e}")
        return

    ai_category = result.get("category", "other").lower()
    category = forced_category or ai_category
    if category not in NOTION_DB:
        category = "other"

    # Groq classified as game → branch to game flow
    if category == "game":
        await handle_save_game_link(chat_id, url, clean_note, meta)
        return

    name      = result.get("name", meta.get("title", ""))[:200] or url
    location  = _clean_field(result.get("location", ""))
    type_     = _clean_field(result.get("type", ""))
    vibe      = _clean_field(result.get("vibe", ""))
    best_for  = _clean_field(result.get("best_for", ""))
    rating    = _clean_field(result.get("rating", ""))
    maps_link = meta.get("maps_url") or result.get("maps_link", "")
    summary   = result.get("summary", "")[:600]
    # Strip "unspecified" language from summaries too
    summary   = re.sub(r'\b(at an? )?unspecified (location|place|address)\b[,.]?', '', summary, flags=re.IGNORECASE).strip()
    if clean_note:
        summary = clean_note + (" — " + summary if summary else "")

    search_terms = result.get("search_terms", [name.split()[0]] if name else [])
    related_pages = await notion_find_related(search_terms) if search_terms else []

    pid = str(uuid.uuid4())[:8]
    PENDING[pid] = {
        "action":        "save_link",
        "chat_id":       chat_id,
        "url":           url,
        "name":          name,
        "category":      category,
        "notes":         summary,
        "forced":        forced_category,
        "ai_category":   ai_category,
        "related_pages": related_pages,
        "maps_link":     maps_link,
        # Extra fields for island DB insert
        "type_":         type_,
        "location":      location,
        "rating":        rating,
        "vibe":          vibe,
        "best_for":      best_for,
    }

    emoji = CATEGORY_EMOJI.get(category, "📌")
    lines = [f"🔍 *{name}*"]
    if location:
        lines.append(f"📍 {location}")
    meta_parts = []
    if type_:    meta_parts.append(type_)
    if vibe:     meta_parts.append(vibe)
    if rating:   meta_parts.append(f"⭐ {rating}")
    if meta_parts:
        lines.append("  ".join(meta_parts))
    lines.append("")
    if summary:
        lines.append(summary)
    if best_for:
        lines.append(f"\n✅ *Best for:* {best_for}")
    if maps_link:
        lines.append(f"\n[📌 View on Google Maps]({maps_link})")
    lines.append(f"\n{emoji} Suggested category: *{category.title()}*")

    text = "\n".join(lines)

    keyboard = []
    if related_pages:
        text += "\n\n*Found in your Notion:*"
        for p in related_pages[:2]:
            text += f"\n• {p['title']}"
        row = [{"text": f"➕ Add to \"{p['title'][:20]}\"",
                "callback_data": f"addto:{pid}:{p['id']}"}
               for p in related_pages[:2]]
        keyboard.append(row)

    keyboard.append([
        {"text": f"💾 Save as {category.title()}",  "callback_data": f"save:{pid}"},
        {"text": "📄 New Notion page",              "callback_data": f"newpage:{pid}"},
    ])
    keyboard.append([
        {"text": "🔄 Change category", "callback_data": f"change:{pid}"},
        {"text": "❌ Cancel",          "callback_data": f"cancel:{pid}"},
    ])

    text += "\n\n_Or just describe what you want._"
    await tg_send_buttons(chat_id, text, keyboard)


async def _do_save_link(chat_id: int, pending: dict, message_id: int | None = None):
    if pending.get("category") == "game":
        await _do_save_game(chat_id, pending, message_id)
        return

    url      = pending["url"]
    category = pending["category"]
    name     = pending["name"]
    notes    = pending["notes"]
    forced   = pending["forced"]
    ai_cat   = pending["ai_category"]

    try:
        notion_url = await notion_save_page(NOTION_DB[category], {
            "Name": {"title": [{"text": {"content": name or url}}]},
            "URL":  {"url": url},
            **({"Notes": {"rich_text": _rich_text(notes)}} if notes else {}),
        })
    except Exception as e:
        await tg_send(chat_id, f"❌ Failed to save: {e}")
        await save_log(url, notes, forced, ai_cat, category, name, str(e))
        return

    await save_log(url, notes, forced, ai_cat, category, name, "✓ success")
    emoji = CATEGORY_EMOJI[category]
    reply = f"{emoji} *Saved to {category.title()}s*\n*{name}*"
    if notes:
        reply += f"\n📝 {notes}"
    reply += f"\n\n[Open in Notion]({notion_url})"

    if message_id:
        await tg_edit_buttons(chat_id, message_id, reply)
    else:
        await tg_send(chat_id, reply)


async def handle_callback_query(cq: dict):
    cq_id      = cq["id"]
    data       = cq.get("data", "")
    chat_id    = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    user_id    = str(cq["from"]["id"])

    await tg_answer_callback(cq_id)

    if user_id != TELEGRAM_USER_ID:
        return

    if data.startswith("hype:"):
        # hype:{pid}:{level} — set hype on a game pending item then save
        parts = data.split(":", 2)
        pid, level = parts[1], parts[2] if len(parts) > 2 else "2"
        pending = PENDING.pop(pid, None)
        if not pending:
            await tg_edit_buttons(chat_id, message_id, "⏱ Action expired — send the link again.")
            return
        hype_map = {"3": "★★★", "2": "★★", "1": "★"}
        pending["hype"] = hype_map.get(level, "★★")
        await tg_edit_buttons(chat_id, message_id, cq["message"]["text"] + "\n\n_Saving…_")
        await _do_save_link(chat_id, pending, message_id)
        return

    if data.startswith("save:"):
        pid = data[5:]
        pending = PENDING.pop(pid, None)
        if not pending:
            await tg_edit_buttons(chat_id, message_id, "⏱ Action expired — send the link again.")
            return
        await tg_edit_buttons(chat_id, message_id, cq["message"]["text"] + "\n\n_Saving…_")
        await _do_save_link(chat_id, pending, message_id)

    elif data.startswith("cancel:"):
        pid = data[7:]
        PENDING.pop(pid, None)
        await tg_edit_buttons(chat_id, message_id, cq["message"]["text"] + "\n\n_Cancelled._")

    elif data.startswith("change:"):
        pid = data[7:]
        if pid not in PENDING:
            await tg_edit_buttons(chat_id, message_id, "⏱ Action expired.")
            return
        keyboard = []
        row = []
        for cat, emoji in CATEGORY_EMOJI.items():
            row.append({"text": f"{emoji} {cat.title()}", "callback_data": f"setcat:{pid}:{cat}"})
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        await tg_edit_buttons(chat_id, message_id, "Choose a category:", keyboard)

    elif data.startswith("setcat:"):
        _, pid, new_cat = data.split(":", 2)
        pending = PENDING.get(pid)
        if not pending:
            await tg_edit_buttons(chat_id, message_id, "⏱ Action expired.")
            return
        pending["category"] = new_cat
        pending["forced"]   = new_cat
        emoji = CATEGORY_EMOJI.get(new_cat, "📌")
        text = (
            f"*{pending['name']}*\n"
            f"{emoji} Category: *{new_cat.title()}*\n\n"
            f"{pending['notes']}\n\n"
            f"Save to Notion?"
        )
        keyboard = [[
            {"text": "✅ Save",   "callback_data": f"save:{pid}"},
            {"text": "❌ Cancel", "callback_data": f"cancel:{pid}"},
        ]]
        await tg_edit_buttons(chat_id, message_id, text, keyboard)

    elif data.startswith("addto:"):
        _, pid, page_id = data.split(":", 2)
        pending = PENDING.pop(pid, None)
        if not pending:
            await tg_edit_buttons(chat_id, message_id, "⏱ Action expired.")
            return
        page_title = next(
            (p["title"] for p in pending.get("related_pages", []) if p["id"] == page_id), "page")
        await tg_edit_buttons(chat_id, message_id,
                              cq["message"]["text"] + f"\n\n_Adding to \"{page_title}\"…_")
        try:
            # Check if the target is a database (island DB) or a plain page
            target_obj = next((p.get("object","page") for p in pending.get("related_pages",[]) if p["id"]==page_id), "page")
            if target_obj == "database":
                notion_url = await insert_into_trip_db(page_id, pending)
            else:
                notion_url = await notion_append_to_page(
                    page_id, pending["name"], pending["url"], pending["notes"],
                    pending.get("type_", ""), pending.get("location", ""), pending.get("rating", ""))
            await save_log(pending["url"], pending["notes"], pending["forced"],
                           pending["ai_category"], pending["category"], pending["name"], "✓ success")
            await tg_edit_buttons(chat_id, message_id,
                f"✅ *Added to \"{page_title}\"*\n\n*{pending['name']}*\n\n[Open in Notion]({notion_url})")
        except Exception as e:
            await tg_send(chat_id, f"❌ Failed: {e}")

    elif data.startswith("newpage:"):
        pid = data[8:]
        pending = PENDING.pop(pid, None)
        if not pending:
            await tg_edit_buttons(chat_id, message_id, "⏱ Action expired.")
            return
        title   = pending["name"]
        content = pending["notes"]
        if pending["url"]:
            content = (content + "\n\n" if content else "") + pending["url"]
        try:
            notion_url = await notion_create_standalone_page(title, content)
            await save_log(pending["url"], pending["notes"], pending["forced"],
                           pending["ai_category"], "other", title, "✓ success (new page)")
            await tg_edit_buttons(chat_id, message_id,
                f"📄 *New page created*\n\n*{title}*\n\n[Open in Notion]({notion_url})")
        except Exception as e:
            await tg_send(chat_id, f"❌ Failed to create page: {e}")

    elif data.startswith("note:"):
        pid = data[5:]
        pending = PENDING.pop(pid, None)
        if not pending:
            await tg_edit_buttons(chat_id, message_id, "⏱ Action expired.")
            return
        title = pending["title"]
        body  = pending["body"]
        results = []
        try:
            notion_url = await notion_save_page(NOTION_DB["other"], {
                "Name":  {"title": [{"text": {"content": title}}]},
                "URL":   {"url": "https://placeholder.com"},
                "Notes": {"rich_text": _rich_text(body)},
            })
            results.append(f"[Notion]({notion_url})")
        except Exception as e:
            results.append(f"Notion ❌ {e}")
        if GITHUB_TOKEN:
            try:
                gh_url = await obsidian_create_note(title, body)
                results.append(f"[Obsidian]({gh_url})")
            except Exception as e:
                results.append(f"Obsidian ❌ {e}")
        reply = f"📝 *Note saved*\n*{title}*\n\n" + " · ".join(results)
        await tg_edit_buttons(chat_id, message_id, reply)


async def handle_pending_modification(chat_id: int, text: str, pid: str, pending: dict):
    # Deterministic fast path: "save it in <topic>" / "add to <topic>" etc.
    # Skip the Groq classifier entirely — it was misreading these as plain "save".
    forced_topic = _detect_addto_intent(text)
    if forced_topic:
        result = {"action": "addto", "page": forced_topic}
    else:
        current = json.dumps({
            "name": pending.get("name", ""),
            "category": pending.get("category", ""),
            "notes": pending.get("notes", ""),
        })
        prompt = (
            f"The user has a pending save action:\n{current}\n\n"
            f"The user said: \"{text}\"\n\n"
            f"Determine their intent. Return ONLY valid JSON:\n"
            f'{{"action": "save", "category": "...", "name": "...", "notes": "...", "page": ""}}\n'
            f"action must be one of: save, addto, cancel, modify\n"
            f"- save: user said ok/yes/save/go ahead — keep all current values\n"
            f"- addto: user wants to save to a SPECIFIC named page/list/topic. "
            f"Examples: 'save to Sifnos', 'save it in Tokyo', 'add to my Amorgos list', "
            f"'put it under Japan trip', 'store this in Tokyo'. Set page to the topic name (no 'my' or 'list').\n"
            f"- cancel: user wants to cancel\n"
            f"- modify: user wants to change something — update only the mentioned fields, keep others unchanged\n"
            f"Valid categories: location, product, article, video, recipe, game, other"
        )
        try:
            raw = await groq_chat([{"role": "user", "content": prompt}], max_tokens=200)
            raw = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.IGNORECASE)
            raw = re.sub(r"```$", "", raw.strip())
            result = json.loads(_extract_json(raw))
        except Exception:
            await tg_send(chat_id, "Didn't understand that — use the buttons or say *save*, *cancel*, or describe a change.")
            return

    action = result.get("action", "modify")

    if action == "cancel":
        PENDING.pop(pid, None)
        await tg_send(chat_id, "❌ Cancelled.")
        return

    if action == "addto":
        page_query = result.get("page", "").strip()
        if not page_query:
            action = "save"  # fall through
        else:
            pending_item = PENDING.pop(pid, None)
            if not pending_item:
                return
            await tg_send(chat_id, f"➕ Adding to \"{page_query}\"…")
            try:
                pages = await notion_search(page_query)
                target = next((p for p in pages if page_query.lower() in p["title"].lower()), None)
                target = target or (pages[0] if pages else None)
                if not target:
                    # Nothing matched — auto-create a topic DB under "Bucket list" if it exists
                    bucket_id = await notion_find_bucket_list()
                    if not bucket_id:
                        await tg_send(chat_id,
                            f"❌ Couldn't find \"{page_query}\" in Notion, and no \"Bucket list\" page either.")
                        return
                    topic_title = _clean_topic_name(page_query) or page_query
                    try:
                        new_db_id = await notion_create_topic_db(bucket_id, topic_title)
                    except Exception as e:
                        await tg_send(chat_id, f"❌ Could not create new DB for \"{topic_title}\": {e}")
                        return
                    notion_url = await insert_into_trip_db(new_db_id, pending_item)
                    await save_log(pending_item["url"], pending_item["notes"], pending_item.get("forced"),
                                   pending_item.get("ai_category", ""), pending_item.get("category", ""),
                                   pending_item["name"], "✓ success (new bucket-list DB)")
                    await tg_send(chat_id,
                        f"📁 Created new database *{topic_title}* under Bucket list and saved "
                        f"*{pending_item['name']}* in it.\n\n[Open in Notion]({notion_url})")
                    return
                label = target["title"]
                if target.get("object") == "database":
                    notion_url = await insert_into_trip_db(target["id"], pending_item)
                else:
                    notion_url = await notion_append_to_page(
                        target["id"], pending_item["name"], pending_item["url"], pending_item["notes"],
                        pending_item.get("type_", ""), pending_item.get("location", ""), pending_item.get("rating", ""))
                await save_log(pending_item["url"], pending_item["notes"], pending_item.get("forced"),
                               pending_item.get("ai_category", ""), pending_item.get("category", ""),
                               pending_item["name"], "✓ success (addto)")
                await tg_send(chat_id,
                    f"✅ *Added to \"{label}\"*\n\n*{pending_item['name']}*\n\n[Open in Notion]({notion_url})")
            except Exception as e:
                await tg_send(chat_id, f"❌ Failed: {e}")
            return

    if action == "save":
        PENDING.pop(pid, None)
        await _do_save_link(chat_id, pending)
        return

    new_cat = result.get("category", "").lower()
    if new_cat and new_cat in NOTION_DB:
        pending["category"] = new_cat
        pending["forced"] = new_cat
    if result.get("name"):
        pending["name"] = result["name"]
    if result.get("notes") is not None and result["notes"] != pending.get("notes"):
        pending["notes"] = result["notes"]

    emoji = CATEGORY_EMOJI.get(pending["category"], "📌")
    preview = (
        f"*Updated*\n\n"
        f"*{pending['name']}*\n"
        f"{emoji} Category: *{pending['category'].title()}*\n\n"
        f"{pending['notes']}\n\n"
        f"Save to Notion?\n\n"
        f"_Or keep describing changes._"
    )
    keyboard = [[
        {"text": "✅ Save",            "callback_data": f"save:{pid}"},
        {"text": "🔄 Change category", "callback_data": f"change:{pid}"},
        {"text": "❌ Cancel",          "callback_data": f"cancel:{pid}"},
    ]]
    await tg_send_buttons(chat_id, preview, keyboard)


async def handle_search(chat_id: int, query: str):
    await tg_send(chat_id, f"🔍 Searching for *{query}*…")
    lines = [f"*Results for \"{query}\":*\n"]
    found = False

    try:
        notion_results = await notion_search(query)
        if notion_results:
            found = True
            lines.append("*Notion:*")
            for r in notion_results:
                line = f"📋 [{r['title']}]({r['notion_url']})"
                if r.get("url"):
                    line += f" — [source]({r['url']})"
                lines.append(line)
    except Exception as e:
        lines.append(f"Notion search failed: {e}")

    if GITHUB_TOKEN:
        try:
            obsidian_results = await obsidian_search(query)
            if obsidian_results:
                found = True
                lines.append("\n*Obsidian:*")
                for r in obsidian_results:
                    lines.append(f"📝 [{r['title']}]({r['url']})")
        except Exception:
            pass

    if not found:
        await tg_send(chat_id, f"No results found for *{query}*.")
        return
    await tg_send(chat_id, "\n".join(lines))


async def handle_list(chat_id: int, category: str | None = None):
    label = category.title() if category else "all"
    await tg_send(chat_id, "📋 Loading recent saves…")
    try:
        pages = await notion_list_recent(category, limit=10)
    except Exception as e:
        await tg_send(chat_id, f"❌ Failed to fetch: {e}")
        return
    if not pages:
        await tg_send(chat_id, "Nothing saved yet.")
        return
    lines = [f"*Recent saves ({label}):*\n"]
    for p in pages:
        emoji = CATEGORY_EMOJI.get(p["category"], "📌")
        lines.append(f"{emoji} [{p['title']}]({p['notion_url']}) — _{p['time']}_")
    await tg_send(chat_id, "\n".join(lines))


async def handle_setupdb(chat_id: int):
    """Create the Trip Places database in Notion and return its ID."""
    if NOTION_DB_TRIP_PLACES:
        await tg_send(chat_id,
            f"✅ Trip Places database is already configured\\.\n\n"
            f"Database ID: `{NOTION_DB_TRIP_PLACES}`")
        return
    await tg_send(chat_id, "⚙️ Creating Trip Places database in Notion…")
    try:
        db_id = await notion_create_trip_db()
        msg = (
            f"✅ *Trip Places database created\\!*\n\n"
            f"Database ID:\n`{db_id}`\n\n"
            f"*Next steps:*\n"
            f"1\\. Go to Vercel → your project → Settings → Environment Variables\n"
            f"2\\. Add: `NOTION_DB_TRIP_PLACES` = `{db_id}`\n"
            f"3\\. Redeploy \\(or push any commit\\)\n\n"
            f"After that, saying *\"save in my Sifnos list\"* will add a row to this "
            f"database tagged with Trip=Sifnos instead of appending to a page\\."
        )
        await tg_send(chat_id, msg)
    except Exception as e:
        await tg_send(chat_id, f"❌ Failed to create database: {e}")


async def handle_create_note(chat_id: int, content: str):
    if ":" in content:
        title, body = content.split(":", 1)
        title, body = title.strip(), body.strip()
    else:
        title = content[:60].rstrip()
        body  = content

    pid = str(uuid.uuid4())[:8]
    PENDING[pid] = {"action": "note", "title": title, "body": body}

    text = (
        f"📝 *Note preview*\n\n"
        f"*{title}*\n\n"
        f"{body}\n\n"
        f"Save to Notion & Obsidian?"
    )
    keyboard = [[
        {"text": "✅ Save",   "callback_data": f"note:{pid}"},
        {"text": "❌ Cancel", "callback_data": f"cancel:{pid}"},
    ]]
    await tg_send_buttons(chat_id, text, keyboard)


async def _handle_list_query(chat_id: int, topic: str, type_filter: str | None) -> bool:
    """Try to answer 'what do I have in <topic>' as a clean list of DB rows.

    Returns True if it handled the query (sent a Telegram message), False if it
    couldn't find a matching DB and the caller should fall through to chat.
    """
    if not topic:
        return False
    type_norm = _map_type(type_filter) if type_filter else None
    try:
        # 1. Find a database whose title matches the topic
        results = await notion_search(topic)
        db_target = next(
            (r for r in results
             if r.get("object") == "database" and topic.lower() in (r.get("title") or "").lower()),
            None,
        )
        # 2. Fall back to ANY database whose title matches the topic
        if not db_target:
            db_target = next((r for r in results if r.get("object") == "database"), None)
        if not db_target:
            return False
        rows = await notion_query_db_rows(db_target["id"], limit=50, type_filter=type_norm)
        if not rows:
            label = db_target.get("title", topic)
            filt_suffix = f" of type *{type_norm}*" if type_norm else ""
            await tg_send(chat_id, f"📭 *{label}* has no items{filt_suffix} yet.")
            return True
        label = db_target.get("title", topic)
        filt_suffix = f" — {type_norm}s only" if type_norm else ""
        lines = [f"*{label}*{filt_suffix} ({len(rows)} items):"]
        for i, row in enumerate(rows, start=1):
            line = f"{i}. *{row['name']}*"
            extras = []
            if row.get("type") and not type_norm:
                extras.append(row["type"])
            if row.get("location"):
                extras.append(row["location"])
            if row.get("rating"):
                extras.append(f"⭐ {row['rating']}")
            if extras:
                line += " — " + ", ".join(extras)
            lines.append(line)
        await tg_send(chat_id, "\n".join(lines))
        return True
    except Exception:
        return False


async def handle_chat(chat_id: int, text: str):
    # Maintain conversation history
    history = CHAT_HISTORY.setdefault(chat_id, [])
    history.append({"role": "user", "content": text})
    if len(history) > MAX_HISTORY:
        CHAT_HISTORY[chat_id] = history[-MAX_HISTORY:]
    history = CHAT_HISTORY[chat_id]

    # Intent classifier: detect "what do I have in X" / "show me X" / list-style queries.
    # These get a clean numbered list instead of AI prose.
    try:
        intent_raw = await groq_chat(
            [{"role": "user", "content":
                "Classify the user's message. Return ONLY JSON, no prose.\n"
                'Schema: {"intent": "list" | "chat", "topic": "<place or topic or null>", '
                '"type_filter": "<one of: Restaurant, Bar, Cafe, Beach, Hotel, Village, Museum, Shop, Sight, Place, Event, Festival, Activity, or null>"}\n'
                "Use intent='list' ONLY for questions like 'what do I have in <place>', "
                "'show me my <X> in <place>', 'list <X> in <place>', or 'what's on my <place> list'. "
                "If the user is asking a general question or follow-up, use intent='chat'.\n\n"
                f"Message: {text}"}],
            max_tokens=80,
        )
        intent_raw = re.sub(r"^```[a-z]*\n?", "", intent_raw.strip(), flags=re.IGNORECASE)
        intent_raw = re.sub(r"```$", "", intent_raw.strip())
        intent = json.loads(_extract_json(intent_raw))
    except Exception:
        intent = {"intent": "chat"}

    if intent.get("intent") == "list" and intent.get("topic"):
        handled = await _handle_list_query(
            chat_id,
            topic=intent["topic"],
            type_filter=intent.get("type_filter"),
        )
        if handled:
            # Don't pollute conversation history with list results — they're complete answers
            return

    # Extract TWO search angles in parallel and union the results.
    # - question_kw: keywords from the current question (catches direct title matches)
    # - topic_kw:    main subject of conversation (catches follow-ups like "And then?" / "How much?")
    # Notion's search API does NOT reliably index content inside table blocks, so a question like
    # "How much is the ferry?" by itself rarely finds the right page; the topic search is what
    # gets us to the trip page where we can then read tables + parents + siblings.
    conversation_context = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history[-6:])

    async def _extract_kw(prompt: str, max_tokens: int) -> str:
        try:
            out = await groq_chat([{"role": "user", "content": prompt}], max_tokens=max_tokens)
            return (out or "").strip()
        except Exception:
            return ""

    question_prompt = (
        "Based on this conversation, extract 2-4 Notion search keywords that would find relevant content.\n"
        "Focus on the TOPIC being discussed (places, events, dates, names), not filler words.\n"
        "Return ONLY the keywords, nothing else.\n\n"
        f"Conversation:\n{conversation_context}"
    )
    topic_prompt = (
        "What is the main subject of this conversation? "
        "Return 1-3 keywords that best describe the SUBJECT (e.g. 'Summer 2026 trip', 'concert', 'recipe'), "
        "not the latest question. Return ONLY the keywords, nothing else.\n\n"
        f"{conversation_context}"
    )

    question_kw, topic_kw = await asyncio.gather(
        _extract_kw(question_prompt, 40),
        _extract_kw(topic_prompt, 20),
    )
    question_kw = question_kw or text
    queries = [q for q in {question_kw, topic_kw} if q]

    # Run all searches in parallel, union by id preserving order
    notion_results: list = []
    context_lines: list = []
    try:
        search_results = await asyncio.gather(
            *[notion_search(q) for q in queries], return_exceptions=True
        )
        seen_ids: set = set()
        for res in search_results:
            if isinstance(res, Exception):
                continue
            for r in res:
                rid = r.get("id", "")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    notion_results.append(r)
        if notion_results:
            context_lines.append("Relevant Notion pages found:")
            for r in notion_results[:8]:
                line = f"- {r['title']}"
                if r.get("url"):
                    line += f" ({r['url']})"
                context_lines.append(line)
    except Exception:
        pass

    # Diagnostic log line — one per question, visible in Vercel logs
    print(f"[chat] queries={queries!r} top={[r.get('title','') for r in notion_results[:5]]}")

    # Read the CONTENT of the top relevant pages (up to 4)
    # For databases (island/trip DBs), query rows instead of reading page blocks
    pages_read = 0
    for page in notion_results[:6]:
        if not page.get("id") or pages_read >= 4:
            continue
        try:
            if page.get("object") == "database":
                rows = await notion_query_db_rows(page["id"], limit=20)
                if rows:
                    context_lines.append(f"\nPlaces in '{page['title']}':")
                    for row in rows:
                        line = f"- {row['name']}"
                        if row.get("type"):     line += f" ({row['type']})"
                        if row.get("location"): line += f" — {row['location']}"
                        if row.get("rating"):   line += f" ⭐ {row['rating']}"
                        context_lines.append(line)
                    pages_read += 1
            else:
                content = await notion_read_page_content(page["id"], max_chars=8000)
                if content and len(content) > 30:
                    context_lines.append(f"\nContent of '{page['title']}':\n{content}")
                    pages_read += 1
        except Exception:
            pass

    # Traverse parent pages + their siblings
    # e.g. "Amorgos" DB lives inside "Summer 2026" → read Summer 2026 content + all sibling pages (Rhodes, Italy…)
    seen_parents: set = set()
    seen_children: set = set(p.get("id", "") for p in notion_results)
    for page in notion_results[:6]:
        if not page.get("id"):
            continue
        try:
            page_meta = await notion_fetch_page_meta(page["id"])
            parent = page_meta.get("parent", {})
            if parent.get("type") == "page_id":
                parent_id = parent["page_id"]
                if parent_id not in seen_parents:
                    seen_parents.add(parent_id)
                    parent_meta = await notion_fetch_page_meta(parent_id)
                    parent_title = parent_meta.get("title", "")
                    if parent_title:
                        parent_content = await notion_read_page_content(parent_id, max_chars=8000)
                        if parent_content and len(parent_content) > 30:
                            context_lines.append(
                                f"\nParent page '{parent_title}' (contains '{page['title']}'):\n{parent_content}"
                            )
                        # Also read sibling pages (other children of the same parent)
                        try:
                            siblings = await notion_get_child_pages(parent_id)
                            sib_added = 0
                            for sib in siblings[:10]:
                                sid = sib["id"]
                                if sid in seen_children or sib_added >= 5:
                                    continue
                                seen_children.add(sid)
                                sib_content = await notion_read_page_content(sid, max_chars=4000)
                                if sib_content and len(sib_content) > 20:
                                    context_lines.append(
                                        f"\nSibling page '{sib['title']}' (inside '{parent_title}'):\n{sib_content}"
                                    )
                                    sib_added += 1
                        except Exception:
                            pass
        except Exception:
            pass

    # Also traverse child pages of direct search results
    for page in notion_results[:2]:
        if not page.get("id"):
            continue
        try:
            children = await notion_get_child_pages(page["id"])
            added = 0
            for child in children[:8]:
                cid = child["id"]
                if cid in seen_children or added >= 5:
                    continue
                seen_children.add(cid)
                child_content = await notion_read_page_content(cid, max_chars=4000)
                if child_content and len(child_content) > 20:
                    context_lines.append(
                        f"\nChild page '{child['title']}' (inside '{page['title']}'):\n{child_content}"
                    )
                    added += 1
        except Exception:
            pass

    # Also pull recent saves for general context
    try:
        recent = await notion_list_recent(limit=6)
        if recent:
            context_lines.append("\nRecently bookmarked links (saved dates are when the link was bookmarked, NOT when the user visited):")
            for p in recent:
                context_lines.append(f"- [{p['category']}] {p['title']} — bookmarked {p['time']}")
    except Exception:
        pass

    knowledge_context = "\n".join(context_lines) if context_lines else "No items found in Notion."
    print(f"[chat] knowledge_chars={len(knowledge_context)} ctx_lines={len(context_lines)}")

    system = (
        "You are a personal knowledge assistant with direct access to the user's Notion.\n\n"
        f"NOTION DATA:\n{knowledge_context}\n\n"
        "Rules:\n"
        "- Answer using the Notion data above. Be specific, reference actual items.\n"
        "- For follow-up questions (e.g. 'And then?', 'What about after that?', 'And after?'), "
        "use the conversation history to understand the topic — stay focused on that topic (e.g. trip itinerary).\n"
        "- 'Recently bookmarked links' are web links the user saved for later — they are NOT a travel history, "
        "NOT visit records, and their dates are bookmark dates, not visit dates. Never use them to answer itinerary questions.\n"
        "- If something isn't in the data, say so clearly. Do NOT invent or guess.\n"
        "- Be concise. Plain text only, no markdown.\n"
        "- Output ONLY the final answer. Do not narrate your reasoning, do not "
        "correct yourself mid-sentence, do not say what something is NOT before "
        "saying what it IS. Think silently, then write one clean answer."
    )

    # Build message list with full conversation history for context
    messages = [{"role": "system", "content": system}]
    messages.extend(history[:-1])   # previous turns
    messages.append({"role": "user", "content": text})

    try:
        response = await groq_chat(messages, max_tokens=1024, model="llama-3.3-70b-versatile")
        await tg_send(chat_id, response)
        # Store assistant reply in history
        history.append({"role": "assistant", "content": response})
    except Exception as e:
        await tg_send(chat_id, f"❌ Error: {e}")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/api/telegram")
async def telegram_webhook(req: Request):
    data = await req.json()

    if "callback_query" in data:
        await handle_callback_query(data["callback_query"])
        return {"ok": True}

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    user_id = str(message.get("from", {}).get("id", ""))
    text    = message.get("text", "").strip()

    if user_id != TELEGRAM_USER_ID:
        return {"ok": True}
    if not text or not chat_id:
        return {"ok": True}

    if text in ("/start", "/help"):
        await tg_send(chat_id, HELP_TEXT)

    elif text == "/setupdb":
        await handle_setupdb(chat_id)

    elif text.startswith("/search "):
        await handle_search(chat_id, text[8:].strip())

    elif text.startswith("/list"):
        parts = text.split(None, 1)
        category = parts[1].strip().lower() if len(parts) > 1 else None
        if category and category.endswith("s"):
            category = category[:-1]
        await handle_list(chat_id, category if category in NOTION_DB else None)

    elif text.startswith("/in "):
        # /in <topic>          → show all items in that topic's DB
        # /in <topic> <type>   → filter by type, e.g. "/in sifnos bar"
        rest = text[4:].strip()
        if not rest:
            await tg_send(chat_id, "Usage: `/in <topic>` or `/in <topic> <type>`")
        else:
            # Try splitting off a trailing type token (last word) if it maps to a valid type
            type_filter = None
            topic = rest
            tokens = rest.rsplit(None, 1)
            if len(tokens) == 2:
                maybe_type = tokens[1].rstrip("s")  # strip plural
                if _map_type(maybe_type) and _map_type(maybe_type) != "Other":
                    type_filter = maybe_type
                    topic = tokens[0]
            handled = await _handle_list_query(chat_id, topic, type_filter)
            if not handled:
                await tg_send(chat_id, f"❌ Couldn't find a list/database for *{topic}*.")

    elif text.startswith("/note "):
        await handle_create_note(chat_id, text[6:].strip())

    elif re.search(r'https?://\S+', text):
        url  = re.search(r'https?://\S+', text).group(0)
        note = text.replace(url, "").strip()
        await handle_save_link(chat_id, url, note)

    else:
        pending_entry = next(
            ((pid, p) for pid, p in PENDING.items() if p.get("chat_id") == chat_id),
            None,
        )
        # Questions get routed to chat even when there's a pending action
        is_question = (
            text.strip().endswith("?") or
            bool(re.search(
                r'^\s*(what|when|where|how|show|find|list|do i|have i|tell me|which|who|why)',
                text, re.IGNORECASE))
        )
        if pending_entry and not is_question:
            pid, pending = pending_entry
            await handle_pending_modification(chat_id, text, pid, pending)
        else:
            await handle_chat(chat_id, text)

    return {"ok": True}


@app.get("/api/logs")
async def get_logs(authorization: str = Header(...)):
    if not secrets.compare_digest(authorization, f"Bearer {SECRET_KEY}"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not NOTION_DB_LOGS:
        return {"ok": True, "logs": []}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.notion.com/v1/databases/{NOTION_DB_LOGS}/query",
            headers=NOTION_HEADERS,
            json={"sorts": [{"timestamp": "created_time", "direction": "descending"}],
                  "page_size": 50},
        )
    r.raise_for_status()

    def _txt(props, key):
        items = props.get(key, {}).get("rich_text", [])
        return items[0]["text"]["content"] if items else ""

    logs = []
    for page in r.json()["results"]:
        p = page["properties"]
        logs.append({
            "time":           page["created_time"],
            "url":            p.get("URL", {}).get("url", ""),
            "note":           _txt(p, "Note"),
            "forced":         _txt(p, "Forced Category"),
            "ai_category":    _txt(p, "AI Category"),
            "final_category": _txt(p, "Final Category"),
            "status":         _txt(p, "Status"),
            "name":           (p.get("Name", {}).get("title") or [{}])[0].get("text", {}).get("content", ""),
        })
    return {"ok": True, "logs": logs}


@app.get("/api/health")
async def health():
    return {"ok": True, "v": "igdb-debug-1"}


@app.get("/api/igdb-test")
async def igdb_test(url: str = "", name: str = "Forza Horizon 6"):
    """Debug endpoint: show IGDB result + Notion DB schema."""
    try:
        meta = {}
        if url:
            meta = await fetch_url_meta(url)
            name = meta.get("title", name)
        igdb_result = await igdb_search_game(name)
        analysed    = await analyse_game_link(url or f"https://store.steampowered.com/app/0/{name}/", meta or {"title": name})

        # Fetch the actual Notion DB schema
        notion_schema = {}
        if NOTION_DB_GAME:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"https://api.notion.com/v1/databases/{NOTION_DB_GAME}",
                    headers=NOTION_HEADERS,
                )
            if r.status_code == 200:
                notion_schema = {k: v["type"] for k, v in r.json().get("properties", {}).items()}
            else:
                notion_schema = {"error": r.status_code, "body": r.text}

        return {
            "meta_title":    name,
            "igdb_raw":      igdb_result,
            "analysed":      analysed,
            "notion_schema": notion_schema,
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}
