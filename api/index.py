import os, json, re, secrets, httpx
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

HELP_TEXT = """*Your personal knowledge assistant* 🧠

*Save a link:*
Just send any URL — I'll classify and save it automatically\\.
Add a note: `https://example\\.com great article`
Force category: `https://example\\.com !video`

*Search your knowledge:*
`/search <query>` — search across all your Notion databases

*Recent saves:*
`/list` — show last 10 saves
`/list articles` — filter by category

*Create a note:*
`/note <title>` — create a note in Notion \\(syncs to Obsidian\\)
`/note Meeting recap: we decided to...`

*Chat:*
Just talk to me — I know about your saved content and can help you think through things\\.

*Categories:* 📍 location · 🛍️ product · 📖 article · 🎬 video · 🍳 recipe · 📌 other"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rich_text(value: str) -> list:
    return [{"text": {"content": value[:2000]}}]

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

async def groq_chat(messages: list, max_tokens: int = 512) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "max_tokens": max_tokens, "messages": messages},
        )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ── Notion operations ─────────────────────────────────────────────────────────

async def notion_classify_and_save(url: str, note: str) -> dict:
    prompt = (
        f"Classify this URL into exactly one category and extract a short title.\n"
        f"URL: {url}\n"
        f"{'User note: ' + note if note else ''}\n\n"
        f"Categories:\n"
        f"- location: Google Maps, Apple Maps, addresses, places, restaurants, hotels\n"
        f"- product: Amazon, shopping, e-commerce, any item for sale\n"
        f"- video: YouTube, TikTok, Vimeo, Reels, any video content\n"
        f"- recipe: cooking recipes, food blogs with recipes\n"
        f"- article: blog posts, news, Wikipedia, documentation, any written content\n"
        f"- other: anything that doesn't fit above\n\n"
        f"Return ONLY valid JSON, no markdown:\n"
        f'{{ "category": "...", "name": "page title or place name", "notes": "one sentence description" }}'
    )
    text = await groq_chat([{"role": "user", "content": prompt}], max_tokens=256)
    return json.loads(text)

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
        # Title can be in any property with type "title" (varies by page/db)
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
        results.append({"title": title, "url": url, "notion_url": obj.get("url", "")})
    return results

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
                    # find which category this db belongs to
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
    """Commit a new .md file to the Obsidian GitHub repo."""
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
    """Search file names and content in the Obsidian GitHub repo."""
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
    """List recently committed files in the Obsidian repo."""
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
    forced_category, clean_note = parse_command(note)
    await tg_send(chat_id, "🔍 Classifying…")
    ai_category = "unknown"
    try:
        result = await notion_classify_and_save(url, clean_note)
        ai_category = result.get("category", "other").lower()
    except Exception as e:
        await tg_send(chat_id, f"❌ Classification failed: {e}")
        await save_log(url, note, forced_category, ai_category, "error", "", str(e))
        return

    category = forced_category or ai_category
    if category not in NOTION_DB:
        category = "other"

    name       = result.get("name", "")[:200]
    notes_text = result.get("notes", "")[:500]
    if clean_note:
        notes_text = (clean_note + (" — " + notes_text if notes_text else ""))[:500]

    try:
        notion_url = await notion_save_page(NOTION_DB[category], {
            "Name": {"title": [{"text": {"content": name or url}}]},
            "URL":  {"url": url},
            **({"Notes": {"rich_text": _rich_text(notes_text)}} if notes_text else {}),
        })
    except Exception as e:
        await tg_send(chat_id, f"❌ Failed to save to Notion: {e}")
        await save_log(url, note, forced_category, ai_category, category, name, str(e))
        return

    await save_log(url, note, forced_category, ai_category, category, name, "✓ success")
    emoji = CATEGORY_EMOJI[category]
    forced_tag = " _(forced)_" if forced_category else ""
    reply = f"{emoji} *Saved to {category.title()}s*{forced_tag}\n*{name}*"
    if notes_text:
        reply += f"\n📝 {notes_text}"
    reply += f"\n\n[Open in Notion]({notion_url})"
    await tg_send(chat_id, reply)


async def handle_search(chat_id: int, query: str):
    await tg_send(chat_id, f"🔍 Searching for *{query}*…")
    lines = [f"*Results for \"{query}\":*\n"]
    found = False

    # Search Notion
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

    # Search Obsidian via GitHub
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
        await tg_send(chat_id, f"No results found for *{query}*\\.")
        return
    await tg_send(chat_id, "\n".join(lines))


async def handle_list(chat_id: int, category: str | None = None):
    label = category.title() if category else "all"
    await tg_send(chat_id, f"📋 Loading recent saves…")
    try:
        pages = await notion_list_recent(category, limit=10)
    except Exception as e:
        await tg_send(chat_id, f"❌ Failed to fetch: {e}")
        return
    if not pages:
        await tg_send(chat_id, "Nothing saved yet\\.")
        return
    lines = [f"*Recent saves ({label}):*\n"]
    for p in pages:
        emoji = CATEGORY_EMOJI.get(p["category"], "📌")
        lines.append(f"{emoji} [{p['title']}]({p['notion_url']}) — _{p['time']}_")
    await tg_send(chat_id, "\n".join(lines))


async def handle_create_note(chat_id: int, content: str):
    # Split title from body if user uses "Title: body" format
    if ":" in content:
        title, body = content.split(":", 1)
        title, body = title.strip(), body.strip()
    else:
        title = content[:60].rstrip()
        body = content

    results = []

    # Save to Notion
    try:
        notion_url = await notion_save_page(NOTION_DB["other"], {
            "Name":  {"title": [{"text": {"content": title}}]},
            "URL":   {"url": "https://placeholder.com"},
            "Notes": {"rich_text": _rich_text(body)},
        })
        results.append(f"[Notion]({notion_url})")
    except Exception as e:
        results.append(f"Notion ❌ {e}")

    # Save to Obsidian via GitHub
    if GITHUB_TOKEN:
        try:
            gh_url = await obsidian_create_note(title, body)
            results.append(f"[Obsidian/GitHub]({gh_url})")
        except Exception as e:
            results.append(f"Obsidian ❌ {e}")

    await tg_send(chat_id, f"📝 *Note saved*\n*{title}*\n\n" + " · ".join(results))


async def handle_chat(chat_id: int, text: str):
    system = (
        "You are a personal knowledge assistant. The user has a personal knowledge base with:\n"
        "- Notion databases: locations, products, articles, videos, recipes, and other/notes\n"
        "- Obsidian vault synced from Notion articles (synced manually)\n\n"
        "You help them save, find, and think about their saved content. "
        "Be concise and helpful. If they ask about their saved content you can't directly access "
        "right now, suggest they use /search or /list to find it. "
        "Keep responses under 200 words. Use plain text, no markdown."
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
    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    user_id = str(message.get("from", {}).get("id", ""))
    text    = message.get("text", "").strip()

    if user_id != TELEGRAM_USER_ID:
        return {"ok": True}
    if not text or not chat_id:
        return {"ok": True}

    # Commands
    if text in ("/start", "/help"):
        await tg_send(chat_id, HELP_TEXT)

    elif text.startswith("/search "):
        await handle_search(chat_id, text[8:].strip())

    elif text.startswith("/list"):
        parts = text.split(None, 1)
        category = parts[1].strip().lower() if len(parts) > 1 else None
        # normalize plural → singular
        if category and category.endswith("s"):
            category = category[:-1]
        await handle_list(chat_id, category if category in NOTION_DB else None)

    elif text.startswith("/note "):
        await handle_create_note(chat_id, text[6:].strip())

    elif re.search(r'https?://\S+', text):
        url = re.search(r'https?://\S+', text).group(0)
        note = text.replace(url, "").strip()
        await handle_save_link(chat_id, url, note)

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
