"""
Steam store client. The public storefront API needs no key, and the appid is an
unambiguous identifier — so whenever a game is on Steam this is the most reliable
source for its name, developer, genres, and release date.
"""
from __future__ import annotations
import re
import httpx
from urllib.parse import quote

from .text_utils import norm_name
from . import mapping

# Auxiliary Steam apps to skip when matching a game (playtests, demos, soundtracks…)
_AUX = ("playtest", "demo", "beta", "soundtrack", "ost", "server",
        "sdk", "artbook", "bonus", "dedicated")


def _is_aux(name: str) -> bool:
    low = (name or "").lower()
    return any(x in low for x in _AUX)


def appid_from_url(url: str) -> str:
    """Extract the numeric appid from a store.steampowered.com/app/<id>/ URL."""
    m = re.search(r"store\.steampowered\.com/app/(\d+)", url or "")
    return m.group(1) if m else ""


async def search_appid(game_name: str) -> str:
    """
    Best Steam appid for a game name. Steam's search ranks playtests/demos above
    the real game for short titles, so prefer an exact (normalised) name match and
    skip auxiliary apps. Returns '' on miss.
    """
    if not game_name:
        return ""
    target = norm_name(game_name)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://steamcommunity.com/actions/SearchApps/{quote(game_name)}")
        if r.status_code != 200:
            return ""
        apps = [a for a in (r.json() or [])
                if isinstance(a, dict) and a.get("appid") and a.get("name")]
    except Exception:
        return ""
    # 1. exact normalised match, not an auxiliary app
    for a in apps:
        if norm_name(a["name"]) == target and not _is_aux(a["name"]):
            return a["appid"]
    # 2. exact normalised match even if auxiliary
    for a in apps:
        if norm_name(a["name"]) == target:
            return a["appid"]
    # 3. non-auxiliary partial match (the search target appears in the name)
    for a in apps:
        if not _is_aux(a["name"]) and target and target in norm_name(a["name"]):
            return a["appid"]
    return ""


async def appdetails(appid: str) -> dict:
    """
    Authoritative game data from Steam's store API for a known appid (no key).
    Returns {} for a missing app, a non-game entry, or any error.
    """
    if not appid:
        return {}
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get("https://store.steampowered.com/api/appdetails",
                                 params={"appids": appid, "l": "english"})
        if r.status_code != 200:
            return {}
        entry = (r.json() or {}).get(str(appid), {})
        if not entry.get("success"):
            return {}
        data = entry.get("data", {})
        if data.get("type") != "game":
            return {}
        devs = data.get("developers") or []
        genres = mapping.map_genres(
            [g.get("description", "") for g in data.get("genres", []) if isinstance(g, dict)])
        # Supplement coarse Steam tags with distinctive subgenres from the blurb.
        blob = f"{data.get('name', '')} {data.get('short_description', '')}".lower()
        for kw, g in mapping.DESC_GENRE_HINTS.items():
            if g not in genres and kw in blob:
                genres.append(g)
        rd = data.get("release_date") or {}
        date_str = (rd.get("date") or "").strip()
        exact = mapping.parse_exact_date(date_str)
        return {
            "name":          data.get("name", ""),
            "developer":     devs[0] if devs else "",
            "genres":        genres,
            "platforms":     ["PC"],                       # Steam only reports desktop
            "release_date":  exact,
            "release_human": "" if exact else date_str,
            "coming_soon":   bool(rd.get("coming_soon")),
            "summary":       (data.get("short_description") or "")[:500],
            "store_url":     f"https://store.steampowered.com/app/{appid}/",
        }
    except Exception as e:
        print(f"[steam] appdetails failed: {e}")
        return {}
