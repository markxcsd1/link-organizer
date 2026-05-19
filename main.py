import os, json, httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_KEY = os.environ["NOTION_API_KEY"]

NOTION_DB = {
    "location": "0fc117f5-c0e4-475b-8580-abd8232128bf",
    "product":  "7403ac9b-cd75-481c-a6cf-71c2f3cefc44",
    "article":  "fe9cc32d-ed19-40d9-a99f-ec69ae1d78e3",
    "video":    "0e7bd3ac-9016-4d78-95bd-c6a30d14c70f",
    "recipe":   "e630d63b-738c-4853-8ffc-cd51051b22d1",
    "other":    "461f0e1c-f794-4fec-902a-277f0e220227",
}

CATEGORY_EMOJI = {
    "location": "📍",
    "product":  "🛍️",
    "article":  "📖",
    "video":    "🎬",
    "recipe":   "🍳",
    "other":    "📌",
}

class LinkRequest(BaseModel):
    url: str
    note: str = ""  # optional extra note from user

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
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip()
    return json.loads(text)


async def save_to_notion(url: str, category: str, name: str, notes: str) -> str:
    db_id = NOTION_DB.get(category, NOTION_DB["other"])

    properties: dict = {
        "Name": {"title": [{"text": {"content": name or url}}]},
        "URL":  {"url": url},
    }
    if notes:
        properties["Notes"] = {"rich_text": [{"text": {"content": notes}}]}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.notion.com/v1/pages",
            headers={
                "Authorization": f"Bearer {NOTION_KEY}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            json={"parent": {"database_id": db_id}, "properties": properties},
        )
    r.raise_for_status()
    return r.json()["url"]


@app.post("/classify")
async def classify_link(req: LinkRequest):
    try:
        result = await classify(req.url, req.note)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Claude error: {e}")

    category = result.get("category", "other").lower()
    if category not in NOTION_DB:
        category = "other"

    name  = result.get("name", "")[:200]
    notes = result.get("notes", "")[:500]

    try:
        notion_url = await save_to_notion(req.url, category, name, notes)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Notion error: {e}")

    emoji = CATEGORY_EMOJI[category]
    return {
        "ok": True,
        "message": f"{emoji} Saved to {category.title()}s\n{name}",
        "category": category,
        "name": name,
        "notion_url": notion_url,
    }


@app.get("/health")
async def health():
    return {"ok": True}
