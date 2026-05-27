from __future__ import annotations
import os, json, re, secrets, uuid, httpx
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

CATEGORY_EMOJI = {
    "location": "📍",
    "product":  "🛍️",
    "article":  "📖",
    "video":    "🎬",
    "recipe":   "🍳",
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
        f"Categories: location, product, article, video, recipe, other\n\n"
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
            if r.status_code == 404:
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

async def notion_query_db_rows(db_id: str, limit: int = 20) -> list:
    """Query all rows of an island/trip database."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=NOTION_HEADERS,
            json={"sorts": [{"property": "Name", "direction": "ascending"}], "page_size": limit},
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
}
_VALID_TYPES = {"Restaurant","Bar","Cafe","Beach","Hotel","Village","Museum","Shop","Sight","Other"}

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

async def notion_create_trip_db() -> str:
    """Create the Trip Places database at workspace root and return its ID."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.notion.com/v1/databases",
            headers=NOTION_HEADERS,
            json={
                "parent": {"type": "workspace", "workspace": True},
                "title": [{"type": "text", "text": {"content": "🗺️ Trip Places"}}],
                "properties": {
                    "Name":     {"title": {}},
                    "URL":      {"url": {}},
                    "Maps":     {"url": {}},
                    "Type":     {"select": {"options": [
                        {"name": "Restaurant", "color": "orange"},
                        {"name": "Bar",        "color": "purple"},
                        {"name": "Cafe",       "color": "yellow"},
                        {"name": "Beach",      "color": "blue"},
                        {"name": "Hotel",      "color": "green"},
                        {"name": "Village",    "color": "pink"},
                        {"name": "Museum",     "color": "brown"},
                        {"name": "Shop",       "color": "red"},
                        {"name": "Other",      "color": "default"},
                    ]}},
                    "Location": {"rich_text": {}},
                    "Rating":   {"rich_text": {}},
                    "Notes":    {"rich_text": {}},
                    "Trip":     {"select": {}},
                },
            },
        )
    r.raise_for_status()
    return r.json()["id"]


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

    try:
        result = await notion_analyse_link(url, clean_note, meta)
    except Exception as e:
        await tg_send(chat_id, f"❌ Analysis failed: {e}")
        return

    ai_category = result.get("category", "other").lower()
    category = forced_category or ai_category
    if category not in NOTION_DB:
        category = "other"

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
        f"- addto: user wants to save to a specific named page/list (e.g. 'save to my Sifnos list', 'add to Tokyo'). Set page to the page name.\n"
        f"- cancel: user wants to cancel\n"
        f"- modify: user wants to change something — update only the mentioned fields, keep others unchanged\n"
        f"Valid categories: location, product, article, video, recipe, other"
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
                    await tg_send(chat_id, f"❌ Couldn't find \"{page_query}\" in Notion.")
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


async def handle_chat(chat_id: int, text: str):
    # Maintain conversation history
    history = CHAT_HISTORY.setdefault(chat_id, [])
    history.append({"role": "user", "content": text})
    if len(history) > MAX_HISTORY:
        CHAT_HISTORY[chat_id] = history[-MAX_HISTORY:]
    history = CHAT_HISTORY[chat_id]

    # Extract search keywords from the FULL conversation context, not just the current message
    # This handles follow-ups like "And then?" or "What about after that?"
    conversation_context = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history[-6:])
    search_query = text
    try:
        kw = await groq_chat([{"role": "user", "content":
            f"Based on this conversation, extract 2-4 Notion search keywords that would find relevant content.\n"
            f"Focus on the TOPIC being discussed (places, events, dates, names), not filler words.\n"
            f"Return ONLY the keywords, nothing else.\n\n"
            f"Conversation:\n{conversation_context}"}],
            max_tokens=40)
        if kw:
            search_query = kw.strip()
    except Exception:
        pass

    # Search Notion with extracted keywords.
    # If the current question yields no results (e.g. "ferry price" doesn't match page text),
    # retry with the broader topic from conversation history — the user's question may use
    # different words than what's written in Notion.
    context_lines = []
    notion_results = []
    try:
        notion_results = await notion_search(search_query)
        if not notion_results and conversation_context:
            topic_kw = await groq_chat([{"role": "user", "content":
                f"What is the main topic of this conversation? "
                f"Return 1-3 keywords that best describe the SUBJECT (not the question itself). "
                f"Return ONLY the keywords, nothing else.\n\n{conversation_context}"}],
                max_tokens=20)
            if topic_kw and topic_kw.strip():
                notion_results = await notion_search(topic_kw.strip())
        if notion_results:
            context_lines.append("Relevant Notion pages found:")
            for r in notion_results[:6]:
                line = f"- {r['title']}"
                if r.get("url"):
                    line += f" ({r['url']})"
                context_lines.append(line)
    except Exception:
        pass

    # Read the CONTENT of the top 3 most relevant pages
    # For databases (island/trip DBs), query rows instead of reading page blocks
    pages_read = 0
    for page in notion_results[:3]:
        if not page.get("id") or pages_read >= 3:
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
    for page in notion_results[:3]:
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
        "- Be concise. Plain text only, no markdown."
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
    return {"ok": True}
