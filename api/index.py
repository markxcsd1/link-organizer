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

NOTION_DB_LOGS = os.environ.get("NOTION_DB_LOGS", "")

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

HELP_TEXT = """*Your personal knowledge assistant* 🧠

*Save a link:*
Just send any URL — I'll analyse it and ask before saving\\.
Add a note: `https://example\\.com great article`
Force category: `https://example\\.com !video`

*Search your knowledge:*
`/search <query>` — search across all your Notion databases

*Recent saves:*
`/list` — show last 10 saves
`/list articles` — filter by category

*Create a note:*
`/note Meeting recap: we decided to\\.\\.\\.`

*Chat:*
Just talk to me in natural language — I'll search your knowledge base and answer\\."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rich_text(value: str) -> list:
    return [{"text": {"content": value[:2000]}}]

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

async def groq_chat(messages: list, max_tokens: int = 512) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "max_tokens": max_tokens, "messages": messages},
        )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

async def fetch_page_meta(url: str) -> dict:
    """
    Generic metadata fetch — one request, follow all redirects, then:
    1. oEmbed auto-discovery (YouTube, Vimeo, Twitter, SoundCloud, etc.)
    2. Open Graph tags
    3. Standard <title> / meta description
    4. Google Maps: extract place name from final URL
    """
    title, desc, author, final_url, html = "", "", "", url, ""

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

    # 4. Google Maps — extract place name from final URL
    maps_url = ""
    if re.search(r'(maps\.google\.|google\.[a-z.]+/maps|maps\.app\.goo\.gl)', url + " " + final_url, re.IGNORECASE):
        maps_url = final_url
        place = ""
        pm = re.search(r'/maps/place/([^/@?&#]+)', final_url)
        if pm:
            place = unquote_plus(pm.group(1))
        else:
            qm = re.search(r'[?&]q=([^&#]+)', final_url)
            if qm:
                place = unquote_plus(qm.group(1)).split(',')[0].strip()
        if place and (not title or title.lower() in ("google maps", "maps")):
            title = place[:200]

    return {
        "title": title[:200] if title else "",
        "desc":  desc[:600]  if desc  else "",
        "author": author,
        "final_url": final_url,
        "maps_url": maps_url,
    }


# ── Notion operations ─────────────────────────────────────────────────────────

async def web_lookup(query: str) -> str:
    """DuckDuckGo Instant Answer — free, no API key, good for well-known places."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"})
        if r.status_code == 200:
            d = r.json()
            text = d.get("AbstractText") or d.get("Answer") or ""
            extras = []
            for entry in d.get("Infobox", {}).get("content", []):
                label = entry.get("label", "")
                value = entry.get("value", "")
                if label and value and label.lower() in ("rating","address","phone","hours","price range"):
                    extras.append(f"{label}: {value}")
            if extras:
                text = text + "\n" + "\n".join(extras) if text else "\n".join(extras)
            return text[:600]
    except Exception:
        pass
    return ""

async def notion_analyse_link(url: str, note: str, meta: dict) -> dict:
    """Deep analysis: extract the actual subject, do a web lookup for extra details."""
    title = meta.get("title", "")
    desc  = meta.get("desc", "")

    # If it looks like a place/restaurant/bar/hotel, do an extra web lookup
    web_info = ""
    place_hint = re.search(
        r'(restaurant|bar|cafe|hotel|beach|tavern|bistro|shop|museum|place|spot)',
        title + " " + desc, re.IGNORECASE)
    if place_hint or (note and re.search(r'(restaurant|bar|cafe|hotel|place)', note, re.IGNORECASE)):
        name_after_pin = re.search(r'📍\s*(\S[^,\n]{1,50})', title)
        candidate = name_after_pin.group(1).strip() if name_after_pin else title[:60]
        web_info = await web_lookup(candidate)

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
        f"3. If web info has rating, address, or reviews — include them in the summary.\n"
        f"4. Generate a maps_link: Google Maps search URL for the place (if it's a location).\n\n"
        f"Categories: location, product, article, video, recipe, other\n\n"
        f"Return ONLY valid JSON, no markdown:\n"
        f'{{"category":"...","name":"exact venue/dish/product name","location":"neighbourhood, city, country","type":"e.g. Beach bar, Sushi restaurant, Boutique hotel","vibe":"2-3 adjectives, e.g. laid-back, scenic, upscale","best_for":"what kind of visit/person, e.g. sunset drinks, solo travelers","summary":"3-5 sentences describing the place — what makes it special, atmosphere, must-tries, any review/rating highlights from the available info","rating":"score if found, else empty","maps_link":"https://www.google.com/maps/search/Name+City (URL-encode spaces as +)","search_terms":["term1","term2"]}}'
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
        props = obj.get("properties", {})
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
        results.append({"id": obj.get("id",""), "title": title, "url": url, "notion_url": obj.get("url", "")})
    return results

async def notion_find_related(search_terms: list) -> list:
    seen = set()
    related = []
    for term in search_terms[:3]:
        try:
            pages = await notion_search(term)
            for p in pages[:4]:
                if p["id"] and p["id"] not in seen and p["title"] != "Untitled":
                    seen.add(p["id"])
                    related.append(p)
        except Exception:
            pass
    return related[:4]

async def notion_append_to_page(page_id: str, name: str, link_url: str, summary: str) -> str:
    rich = [{"type": "text", "text": {"content": name,
             "link": {"url": link_url} if link_url else None}}]
    if summary:
        rich.append({"type": "text", "text": {"content": f" — {summary}"}})
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=NOTION_HEADERS,
            json={"children": [{"object":"block","type":"bulleted_list_item",
                                "bulleted_list_item":{"rich_text": rich}}]},
        )
    r.raise_for_status()
    return f"https://www.notion.so/{page_id.replace('-','')}"

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
    location  = result.get("location", "")
    type_     = result.get("type", "")
    vibe      = result.get("vibe", "")
    best_for  = result.get("best_for", "")
    rating    = result.get("rating", "")
    maps_link = meta.get("maps_url") or result.get("maps_link", "")
    summary   = result.get("summary", "")[:600]
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
            notion_url = await notion_append_to_page(
                page_id, pending["name"], pending["url"], pending["notes"])
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
        f'{{"action": "save", "category": "...", "name": "...", "notes": "..."}}\n'
        f"action must be one of: save, cancel, modify\n"
        f"- save: user said ok/yes/save/go ahead — keep all current values\n"
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
    search_query = text
    try:
        kw = await groq_chat([{"role": "user", "content":
            f"Extract 1-3 search keywords from this message. Return ONLY the keywords, nothing else.\n\nMessage: {text}"}],
            max_tokens=30)
        if kw:
            search_query = kw.strip()
    except Exception:
        pass

    context_lines = []
    try:
        notion_results = await notion_search(search_query)
        if notion_results:
            context_lines.append("Relevant items from the user's Notion knowledge base:")
            for r in notion_results[:6]:
                line = f"- {r['title']}"
                if r.get("url"):
                    line += f" ({r['url']})"
                context_lines.append(line)
    except Exception:
        pass

    try:
        recent = await notion_list_recent(limit=5)
        if recent:
            context_lines.append("\nRecently saved:")
            for p in recent:
                context_lines.append(f"- [{p['category']}] {p['title']} — {p['time']}")
    except Exception:
        pass

    knowledge_context = "\n".join(context_lines) if context_lines else "No items found."

    system = (
        "You are a personal knowledge assistant with direct access to the user's Notion knowledge base.\n\n"
        f"{knowledge_context}\n\n"
        "Answer the user's question using the above data. Be specific and reference actual items when relevant. "
        "If the user asks to find something not in the results, tell them to try /search <query>. "
        "Keep responses concise. Use plain text, no markdown formatting."
    )
    try:
        response = await groq_chat([
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ])
        await tg_send(chat_id, response)
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
        if pending_entry:
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
