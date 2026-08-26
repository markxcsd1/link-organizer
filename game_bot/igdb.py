"""
IGDB game database client (authenticated via a free Twitch app).

Used to enrich console platforms (Steam only reports desktop) and as a fallback
for games that aren't on Steam. IGDB is deliberately NOT trusted for the precise
release day: its `first_release_date` back-fills a placeholder day for
year/quarter/TBD entries, so `select_release` only returns an exact date from an
IGDB "exact" (category 0) entry.
"""
from __future__ import annotations
import time
from datetime import datetime, timezone
import httpx

from .config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
from . import mapping

# App-access token cache (Twitch tokens last ~60 days; refreshed when near expiry).
_TOKEN: str = ""
_TOKEN_EXPIRY: float = 0.0


async def _get_token() -> str:
    global _TOKEN, _TOKEN_EXPIRY
    if _TOKEN and time.time() < _TOKEN_EXPIRY - 60:
        return _TOKEN
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        })
    r.raise_for_status()
    data = r.json()
    _TOKEN = data["access_token"]
    _TOKEN_EXPIRY = time.time() + data.get("expires_in", 3600)
    return _TOKEN


def select_release(game: dict) -> tuple[str, str, int | None]:
    """
    Choose the best release date from an IGDB game's `release_dates`. Returns
    (exact_iso, human, ts):
      - exact_iso: 'YYYY-MM-DD' only for an exact (category 0) entry, else ''
      - human:     the readable string of the chosen entry (e.g. 'Q2 2026', '2019')
      - ts:        a unix timestamp for the Out/Unreleased decision (may be approximate)
    """
    PREF_REGIONS = (8, 2)  # 8 = worldwide, 2 = North America
    rds = [rd for rd in game.get("release_dates", []) if isinstance(rd, dict)]

    exact = [rd for rd in rds if rd.get("category") == 0 and rd.get("date")]
    if exact:
        pref = [rd for rd in exact if rd.get("region") in PREF_REGIONS] or exact
        chosen = min(pref, key=lambda rd: rd["date"])
        iso = datetime.fromtimestamp(chosen["date"], tz=timezone.utc).strftime("%Y-%m-%d")
        return iso, chosen.get("human", "") or "", chosen["date"]

    with_ts = [rd for rd in rds if rd.get("date")]
    if with_ts:
        chosen = min(with_ts, key=lambda rd: rd["date"])
        return "", chosen.get("human", "") or "", chosen["date"]

    frd = game.get("first_release_date")
    return "", "", frd if frd else None


async def search_game(name: str) -> dict:
    """Search IGDB for a game by name; return a normalised dict, or {} on a miss."""
    token = await _get_token()
    clean = mapping.clean_game_title(name)
    query = (
        f'search "{clean}"; '
        f"fields name,summary,genres.name,platforms.name,"
        f"involved_companies.company.name,involved_companies.developer,"
        f"first_release_date,websites.url,websites.category,"
        f"release_dates.human,release_dates.category,release_dates.date,release_dates.region; "
        f"limit 1;"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post("https://api.igdb.com/v4/games", content=query, headers={
            "Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}",
        })
    r.raise_for_status()
    results = r.json()
    if not results:
        return {}
    game = results[0]

    # involved_companies can come back as bare ints (unexpanded) — guard with isinstance.
    developer = _pick_developer(game.get("involved_companies", []))
    genres = mapping.map_genres(
        [g.get("name", "") for g in game.get("genres", []) if isinstance(g, dict)])
    platforms = mapping.map_platforms(
        [p.get("name", "") for p in game.get("platforms", []) if isinstance(p, dict)])
    release_date, release_human, release_ts = select_release(game)

    # Store/official website (category: 13=Steam, 17=GOG, 16=Epic, 15=itch, 1=official).
    sites: dict = {}
    for w in game.get("websites", []):
        if isinstance(w, dict) and w.get("url"):
            sites.setdefault(w.get("category"), w["url"])
    store_url = next((sites[c] for c in (13, 17, 16, 15, 1) if c in sites), "")

    return {
        "name":          game.get("name", clean),
        "developer":     developer,
        "genres":        genres,
        "platforms":     platforms,
        "release_date":  release_date,    # exact ISO only (category 0); else ""
        "release_human": release_human,   # approximate text, e.g. "Q2 2026"
        "release_ts":    release_ts,      # best timestamp for the Out/Unreleased call
        "store_url":     store_url,
        "summary":       (game.get("summary") or "")[:500],
    }


def _pick_developer(companies: list) -> str:
    """The company flagged as developer, else the first named company."""
    for ic in companies:
        if isinstance(ic, dict) and ic.get("developer"):
            company = ic.get("company")
            if isinstance(company, dict):
                return company.get("name", "")
    for ic in companies:
        if isinstance(ic, dict):
            company = ic.get("company")
            if isinstance(company, dict) and company.get("name"):
                return company["name"]
    return ""
