"""
Find the pieces the source link didn't provide: a store page, a critic review,
and a trailer. Prefer keyless, deterministic sources (Steam's search API,
Metacritic's slug scheme) and fall back to a DuckDuckGo scrape.
"""
from __future__ import annotations
import re
import httpx
from urllib.parse import unquote

from . import steam

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")

_REVIEW_DOMAINS = (
    "ign.com", "gamespot.com", "pcgamer.com", "eurogamer.net", "polygon.com",
    "rockpapershotgun.com", "metacritic.com", "opencritic.com", "destructoid.com",
    "gamesradar.com", "pcgamesn.com", "videogameschronicle.com", "gameinformer.com",
)


async def find_store_url(game_name: str) -> str:
    """Locate a Steam store page — Steam's app-search API first, DuckDuckGo as fallback."""
    appid = await steam.search_appid(game_name)
    if appid:
        return f"https://store.steampowered.com/app/{appid}/"
    for decoded in await _ddg_links(f"{game_name} steam store"):
        if "store.steampowered.com/app/" in decoded:
            return decoded.split("?")[0]
    return ""


def metacritic_url(game_name: str) -> str:
    """Build the Metacritic game-page URL (slug: lowercase, apostrophes dropped, rest
    hyphenated) — e.g. "Baldur's Gate 3" -> baldurs-gate-3."""
    s = re.sub(r"['’™®©:]", "", (game_name or "").lower())
    slug = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return f"https://www.metacritic.com/game/{slug}/" if slug else ""


async def find_review(game_name: str) -> str:
    """
    Prefer the Metacritic game page (keyless, deterministic slug, aggregates all
    critic scores); verify it resolves, and fall back to a DuckDuckGo lookup for a
    reputable outlet. Returns '' only if nothing pans out.
    """
    mc = metacritic_url(game_name)
    if mc:
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                         headers={"User-Agent": _UA}) as client:
                r = await client.get(mc)
            if r.status_code != 404:        # 200 = ok; 403 = bot-blocked but page exists
                return mc
        except Exception:
            return mc                        # network hiccup — the page still likely exists
    for decoded in await _ddg_links(f"{game_name} review"):
        if any(d in decoded for d in _REVIEW_DOMAINS):
            return decoded.split("?")[0]
    return ""


async def find_trailer(game_name: str) -> str:
    """Search DuckDuckGo for a YouTube trailer; return the first YouTube URL found."""
    for decoded in await _ddg_links(f"{game_name} official trailer youtube"):
        if "youtube.com/watch" in decoded or "youtu.be/" in decoded:
            return decoded
    return ""


async def _ddg_links(query: str) -> list[str]:
    """Return the outbound links from a DuckDuckGo HTML search (decoded, in order)."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={
            "User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9",
        }) as client:
            r = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
        if r.status_code != 200:
            return []
        # DuckDuckGo wraps external URLs as /l/?uddg=<percent-encoded url>.
        return [unquote(m.group(1)) for m in re.finditer(r'uddg=(https?[^&"\']+)', r.text)]
    except Exception:
        return []
