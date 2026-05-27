import os, json, re, secrets, uuid, httpx
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
    """Send a message with inline keyboard buttons."""
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
    """Edit an existing message (to replace buttons after tap)."""
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
    """Fetch real page title + description from HTML meta tags."""
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        html = r.text
        og_title   = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        plain_title = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        og_desc    = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        meta_desc  = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        title = (og_title or plain_title)
        desc  = (og_desc or meta_desc)
        return {
            "title": title.group(1).strip()[:200] if title else "",
            "desc":  desc.group(1).strip()[:400]  if desc  else "",
        }
    except Exception:
        return {"title": "", "desc": ""}


# ── Notion operations ─────────────────────────────────────────────────────────

async def notion_analyse_link(url: str, note: str, meta: dict) -> dict:
    """Ask Groq to classify and summarise a link, using real page metadata."""
    meta_text = ""
    if meta.get("title"):
        meta_text += f"Page title: {meta['title']}\n"
    if meta.get("desc"):
        meta_text += f"Page description: {meta['desc']}\n"

    prompt = (
        f"Analyse this URL for a personal knowledge base.\n"
        f"URL: {url}\n"
        f"{meta_text}"
        f"{'User note: ' + note if note else ''}\n\n"
        f"Categories:\n"
        f"- location: Google Maps, Apple Maps, addresses, places, restaurants, hotels\n"
        f"- product: Amazon, shopping, e-commerce, any item for sale\n"
        f"- video: YouTube, TikTok, Vimeo, Reels, any video content\n"
        f"- recipe: cooking recipes, food blogs with recipes\n"
        f"- article: blog posts, news, Wikipedia, documentation, any written content\n"
        f"- other: anything that doesn't fit above\n\n"
        f"Return ONLY valid JSON, no markdown:\n"
        f'{{ "category": "...", "name": "clean page title or place name", "summary": "2-3 sentence summary of what this is and why it might be useful" }}'
    )
    text = await groq_chat([{"role": "user", "content": prompt}], max_tokens=300)
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

    # Fetch real page metadata
    meta = await fetch_page_meta(url)

    # Ask Groq to classify + summarise
    try:
        result = await notion_analyse_link(url, clean_note, meta)
    except Exception as e:
        await tg_send(chat_id, f"❌ Analysis failed: {e}")
        return

    ai_category = result.get("category", "other").lower()
    category = forced_category or ai_category
    if category not in NOTION_DB:
        category = "other"

    name    = result.get("name", meta.get("title", ""))[:200] or url
    summary = result.get("summary", "")[:400]
    if clean_note:
        summary = clean_note + (" — " + summary if summary else "")

    # Store pending action
    pid = str(uuid.uuid4())[:8]
    PENDING[pid] = {
        "action":      "save_link",
        "chat_id":     chat_id,
        "url":         url,
        "name":        name,
        "category":    category,
        "notes":       summary,
        "forced":      forced_category,
        "ai_category": ai_category,
    }

    emoji = CATEGORY_EMOJI.get(category, "📌")
    text = (
        f"🔍 *Analysis*\n\n"
        f"*{name}*\n"
        f"{emoji} Category: *{category.title()}*\n\n"
        f"{summary}\n\n"
        f"Save to Notion?"
    )
    keyboard = [[
        {"text": "✅ Save",            "callback_data": f"save:{pid}"},
        {"text": "🔄 Change category", "callback_data": f"change:{pid}"},
        {"text": "❌ Cancel",          "callback_data": f"cancel:{pid}"},
    ]]
    await tg_send_buttons(chat_id, text, keyboard)


async def _do_save_link(chat_id: int, pending: dict, message_id: int | None = None):
    """Actually save the link to Notion after confirmation."""
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
    """Handle button taps."""
    cq_id      = cq["id"]
    data       = cq.get("data", "")
    chat_id    = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    user_id    = str(cq["from"]["id"])

    await tg_answer_callback(cq_id)

    if user_id != TELEGRAM_USER_ID:
        return

    # ── Save confirmed ────────────────────────────────────────────────────────
    if data.startswith("save:"):
        pid = data[5:]
        pending = PENDING.pop(pid, None)
        if not pending:
            await tg_edit_buttons(chat_id, message_id,
                                  "⏱ Action expired — send the link again.")
            return
        await tg_edit_buttons(chat_id, message_id,
                              cq["message"]["text"] + "\n\n_Saving…_")
        await _do_save_link(chat_id, pending, message_id)

    # ── Cancelled ─────────────────────────────────────────────────────────────
    elif data.startswith("cancel:"):
        pid = data[7:]
        PENDING.pop(pid, None)
        await tg_edit_buttons(chat_id, message_id,
                              cq["message"]["text"] + "\n\n_Cancelled._")

    # ── Change category — show category picker ────────────────────────────────
    elif data.startswith("change:"):
        pid = data[7:]
        if pid not in PENDING:
            await tg_edit_buttons(chat_id, message_id, "⏱ Action expired.")
            return
        keyboard = []
        row = []
        for cat, emoji in CATEGORY_EMOJI.items():
            row.append({"text": f"{emoji} {cat.title()}",
                        "callback_data": f"setcat:{pid}:{cat}"})
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        await tg_edit_buttons(chat_id, message_id,
                              "Choose a category:", keyboard)

    # ── Category selected ─────────────────────────────────────────────────────
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

    # ── Note confirmed ────────────────────────────────────────────────────────
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
    """Show note preview and ask for confirmation."""
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
    # Extract search keywords from natural language
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

    # Handle button taps
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
