"""Persist a resolved game to the Notion "To Play" database."""
from __future__ import annotations
import httpx

from .config import NOTION_HEADERS, NOTION_DB_GAME
from .text_utils import rich_text

_STATUSES = ("Unreleased", "Out", "Playing", "Finished")
_HYPE = ("★★★", "★★", "★")


async def save_game(game: dict) -> str:
    """Create a page in the To Play database and return its Notion URL."""
    props: dict = {"Name": {"title": [{"text": {"content": (game.get("name") or "")[:200]}}]}}

    for field, key in (("Review", "review_url"), ("Video", "video_url"), ("Store", "store_url")):
        if game.get(key):
            props[field] = {"url": game[key]}
    if game.get("developer"):
        props["Developer"] = {"rich_text": rich_text(game["developer"])}
    if game.get("genres"):
        props["Genre"] = {"multi_select": [{"name": g} for g in game["genres"]]}
    if game.get("platforms"):
        props["Platform"] = {"multi_select": [{"name": p} for p in game["platforms"]]}
    if game.get("status") in _STATUSES:
        props["Status"] = {"select": {"name": game["status"]}}
    if game.get("hype") in _HYPE:
        props["Hype"] = {"select": {"name": game["hype"]}}
    if game.get("release_date"):
        props["Release Date"] = {"date": {"start": game["release_date"]}}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS,
                              json={"parent": {"database_id": NOTION_DB_GAME}, "properties": props})
    r.raise_for_status()
    return r.json()["url"]
