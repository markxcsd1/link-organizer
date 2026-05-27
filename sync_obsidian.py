#!/usr/bin/env python3
"""
Fetches new articles from Notion and creates Obsidian markdown notes.
Run manually whenever you want to sync: python3 sync_obsidian.py
"""

import os, re, httpx
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
def _load_env(key):
    val = os.environ.get(key)
    if val:
        return val
    for fname in (".env", ".env.local"):
        try:
            for line in open(fname):
                line = line.strip()
                if line.startswith(f"{key}=") and not line.endswith('""') and not line.endswith("="):
                    return line.split("=", 1)[1].strip().strip('"')
        except FileNotFoundError:
            pass
    raise SystemExit(f"❌  {key} not found. Add it to .env or export it in your shell.")

NOTION_KEY      = _load_env("NOTION_API_KEY")
NOTION_DB       = _load_env("NOTION_DB_ARTICLE")
VAULT_PATH      = Path("/Users/mark/Library/Mobile Documents/iCloud~md~obsidian/Documents/Mine")
ARTICLES_FOLDER = VAULT_PATH / "Articles"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name.strip()[:100] or "Untitled"

def fetch_articles():
    resp = httpx.post(
        f"https://api.notion.com/v1/databases/{NOTION_DB}/query",
        headers=NOTION_HEADERS,
        json={"sorts": [{"timestamp": "created_time", "direction": "descending"}], "page_size": 100},
    )
    resp.raise_for_status()
    return resp.json()["results"]

def page_to_note(page: dict) -> tuple:
    props = page["properties"]

    title_items = props.get("Name", {}).get("title", [])
    title = title_items[0]["text"]["content"] if title_items else "Untitled"

    url = props.get("URL", {}).get("url", "")

    notes_items = props.get("Notes", {}).get("rich_text", [])
    notes = notes_items[0]["text"]["content"] if notes_items else ""

    created = page.get("created_time", "")[:10]

    return title, url, notes, created

def create_md(title: str, url: str, notes: str, created: str) -> Path:
    ARTICLES_FOLDER.mkdir(parents=True, exist_ok=True)
    filename = sanitize_filename(title) + ".md"
    filepath = ARTICLES_FOLDER / filename

    content = f"""---
url: {url}
saved: {created}
tags: [article]
---

# {title}

"""
    if notes:
        content += f"> {notes}\n\n"

    content += f"[Open original]({url})\n"

    filepath.write_text(content, encoding="utf-8")
    return filepath

def main():
    print("Fetching articles from Notion...")
    articles = fetch_articles()
    created_count = 0

    for page in articles:
        title, url, notes, created = page_to_note(page)
        filename = sanitize_filename(title) + ".md"
        filepath = ARTICLES_FOLDER / filename

        if filepath.exists():
            continue

        path = create_md(title, url, notes, created)
        print(f"  ✓ {path.name}")
        created_count += 1

    if created_count == 0:
        print("  All articles already synced.")
    else:
        print(f"\nCreated {created_count} new note(s) in {ARTICLES_FOLDER}")

if __name__ == "__main__":
    main()
