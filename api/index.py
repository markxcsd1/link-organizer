import os, json, secrets, httpx
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

app = FastAPI()

GROQ_KEY   = os.environ["GROQ_API_KEY"]
NOTION_KEY = os.environ["NOTION_API_KEY"]
SECRET_KEY = os.environ["SECRET_KEY"]

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


class LinkRequest(BaseModel):
    url: str
    note: str = ""


def parse_command(note: str) -> tuple:
    """Extract !category override from note. Returns (forced_category | None, clean_note)."""
    note = note.strip()
    if note.startswith("!"):
        parts = note.split(None, 1)
        cmd = parts[0][1:].lower()
        clean_note = parts[1].strip() if len(parts) > 1 else ""
        if cmd in NOTION_DB:
            return cmd, clean_note
    return None, note


def _rich_text(value: str) -> list:
    return [{"text": {"content": value[:2000]}}]


async def classify(url: str, note: str) -> dict:
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

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    return json.loads(text)


async def save_to_notion(url: str, category: str, name: str, notes: str) -> str:
    db_id = NOTION_DB.get(category, NOTION_DB["other"])
    properties: dict = {
        "Name": {"title": [{"text": {"content": name or url}}]},
        "URL":  {"url": url},
    }
    if notes:
        properties["Notes"] = {"rich_text": _rich_text(notes)}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={"parent": {"database_id": db_id}, "properties": properties},
        )
    r.raise_for_status()
    return r.json()["url"]


async def save_log(url: str, note: str, forced: str | None, ai_category: str, final_category: str, name: str, status: str):
    if not NOTION_DB_LOGS:
        return
    try:
        properties = {
            "Name":             {"title": [{"text": {"content": (name or url)[:200]}}]},
            "URL":              {"url": url},
            "Note":             {"rich_text": _rich_text(note)},
            "Forced Category":  {"rich_text": _rich_text(forced or "")},
            "AI Category":      {"rich_text": _rich_text(ai_category)},
            "Final Category":   {"rich_text": _rich_text(final_category)},
            "Status":           {"rich_text": _rich_text(status)},
        }
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                "https://api.notion.com/v1/pages",
                headers=NOTION_HEADERS,
                json={"parent": {"database_id": NOTION_DB_LOGS}, "properties": properties},
            )
    except Exception:
        pass


@app.post("/api/classify")
async def classify_link(req: LinkRequest, authorization: str = Header(...), note: str = ""):
    if not secrets.compare_digest(authorization, f"Bearer {SECRET_KEY}"):
        await save_log(req.url, "", None, "", "error", "", "401 Unauthorized")
        raise HTTPException(status_code=401, detail="Unauthorized")

    # note can come from JSON body or query param
    combined_note = req.note or note
    forced_category, clean_note = parse_command(combined_note)

    ai_category = "unknown"
    try:
        result = await classify(req.url, clean_note)
        ai_category = result.get("category", "other").lower()
    except Exception as e:
        await save_log(req.url, combined_note, forced_category, ai_category, "error", "", f"Groq error: {e}")
        raise HTTPException(status_code=502, detail=f"Groq error: {e}")

    category = forced_category or ai_category
    if category not in NOTION_DB:
        category = "other"

    name  = result.get("name", "")[:200]
    notes = result.get("notes", "")[:500]
    if clean_note:
        notes = (clean_note + (" — " + notes if notes else ""))[:500]

    try:
        notion_url = await save_to_notion(req.url, category, name, notes)
    except Exception as e:
        await save_log(req.url, combined_note, forced_category, ai_category, category, name, f"Notion error: {e}")
        raise HTTPException(status_code=502, detail=f"Notion error: {e}")

    await save_log(req.url, combined_note, forced_category, ai_category, category, name, "✓ success")

    emoji = CATEGORY_EMOJI[category]
    message = f"{emoji} Saved to {category.title()}s\n{name}"
    if clean_note:
        message += f"\n📝 {clean_note}"
    return {
        "ok": True,
        "message": message,
        "category": category,
        "name": name,
        "notion_url": notion_url,
    }


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
            json={"sorts": [{"timestamp": "created_time", "direction": "descending"}], "page_size": 50},
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
