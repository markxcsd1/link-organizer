"""
analyse_game_link — turn a link + its page metadata into one normalised game dict:

    {name, developer, genres, platforms, release_date, release_human, status,
     store_url, summary}

Resolution strategy, in order of trust:
  1. Steam appdetails — if the game is on Steam (store URL, or found by name), its
     appid is unambiguous, so Steam is authoritative for name/developer/genres/date.
  2. IGDB — enriches console platforms (Steam only reports desktop) and is the
     fallback for games that aren't on Steam.
  3. Groq — last-resort extraction when neither source recognises the game.

A precise release day is only ever written when Steam, an IGDB "exact" entry, or
the store page provides one — never fabricated from a year/quarter/"coming soon".
"""
from __future__ import annotations
from datetime import date, datetime, timezone

from .config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
from .text_utils import clean_field, norm_name, safe_json_loads, strip_fences
from . import mapping, metadata, steam, igdb, discovery, llm

_GENRE_LIST = (
    "Roguelite, Roguelike, Deckbuilder, Metroidvania, Action, Platformer, Survivors-like, "
    "Strategy, RPG, Shooter, Adventure, Racing, Sports, Fighting, Simulation, Puzzle, Horror, "
    "Survival, Stealth, Soulslike, Open World, Sandbox, City Builder, Tower Defense, MOBA, "
    "Battle Royale, MMO, Visual Novel, Rhythm"
)


async def analyse_game_link(url: str, meta: dict) -> dict:
    title, desc = meta.get("title", ""), meta.get("desc", "")

    slug = mapping.game_name_from_url(url)
    search_name = slug if (slug and metadata.is_generic_title(title)) else title
    cleaned = mapping.clean_game_title(search_name) or search_name

    # ── Steam first: appid from the URL, else search Steam by name ───────────
    appid = steam.appid_from_url(url) or await steam.search_appid(cleaned)
    steam_data = await steam.appdetails(appid) if appid else {}

    # ── IGDB: search by Steam's exact name when known (platforms + fallback) ──
    igdb_data = await _igdb_lookup(steam_data.get("name") or cleaned, slug)

    # ── Steam is authoritative when it matched ───────────────────────────────
    if steam_data.get("name"):
        result = dict(steam_data)
        result["status"] = "Unreleased" if result.pop("coming_soon", False) else "Out"
        # Enrich console platforms from IGDB only when it's clearly the same game.
        if igdb_data.get("name") and norm_name(igdb_data["name"]) == norm_name(steam_data["name"]):
            result["platforms"] = list(dict.fromkeys(
                (result.get("platforms") or []) + (igdb_data.get("platforms") or [])))
        result.setdefault("summary", desc[:300] if desc else "")
        result["genres"] = mapping.map_genres(result.get("genres") or [])
        result["platforms"] = mapping.map_platforms(result.get("platforms") or [])
        return result

    # ── Not on Steam → IGDB, else Groq ───────────────────────────────────────
    result = dict(igdb_data) if igdb_data.get("name") else await _groq_fallback(url, cleaned, desc)
    name = clean_field(result.get("name", "")) or cleaned

    # ── Locate the store page (the shared link, IGDB's website, or a search) ──
    store_url = result.get("store_url") or ""
    if not store_url and mapping.is_game_url(url):
        store_url = url
    if not store_url and name:
        store_url = await discovery.find_store_url(name)
    result["store_url"] = store_url

    # ── Authoritative release date: read the store page first ────────────────
    store_date, store_human = "", ""
    if store_url:
        page = await metadata.jina_read(store_url)
        if page.get("content"):
            store_date, store_human = mapping.extract_store_release_date(page["content"])

    release_date = store_date or result.get("release_date") or ""
    release_human = store_human or result.get("release_human") or ""
    # Promote a full specific human date (e.g. "Jun 17, 2026") to an exact date so it
    # gets saved. Year/quarter/"coming soon" stay approximate (never a fabricated day).
    if not release_date and release_human:
        promoted = mapping.parse_exact_date(release_human)
        if promoted:
            release_date, release_human = promoted, ""

    result["name"] = name
    result["release_date"] = release_date              # exact ISO only, else ""
    result["release_human"] = "" if release_date else release_human
    result["status"] = _status(release_date, release_human, result.get("release_ts"),
                                result.get("status"))
    result.setdefault("summary", desc[:300] if desc else "")
    result["genres"] = mapping.map_genres(result.get("genres") or [])
    result["platforms"] = mapping.map_platforms(result.get("platforms") or [])
    return result


def _status(release_date: str, release_human: str, release_ts, groq_status) -> str:
    """Out vs Unreleased, using any signal available (even an approximate timestamp)."""
    if release_date:
        try:
            return "Unreleased" if date.fromisoformat(release_date) > date.today() else "Out"
        except ValueError:
            return "Out"
    if release_ts:
        return "Unreleased" if release_ts > datetime.now(timezone.utc).timestamp() else "Out"
    if release_human:
        return "Unreleased"                            # a quarter/year still ahead
    return groq_status or "Out"


async def _igdb_lookup(query: str, slug: str) -> dict:
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET and query):
        return {}
    try:
        data = await igdb.search_game(query)
        if not data.get("name") and slug and norm_name(slug) != norm_name(query):
            data = await igdb.search_game(slug)
        return data
    except Exception as e:
        print(f"[igdb] search failed: {e}")
        return {}


async def _groq_fallback(url: str, title: str, desc: str) -> dict:
    """Last-resort extraction when neither Steam nor IGDB recognises the game."""
    prompt = (
        "Extract game details from this URL and metadata. Use your knowledge of the game "
        "if you recognise it.\n"
        f"URL: {url}\nTitle: {title}\nDescription: {desc[:500]}\n\n"
        "Return ONLY valid JSON — use empty string for unknown fields, never 'unknown':\n"
        '{"name":"exact game name","developer":"studio name or empty",'
        '"genres":["genre1"],"platforms":["platform1"],'
        '"release_date":"YYYY or YYYY-MM-DD or empty","status":"Unreleased or Out",'
        '"summary":"1-2 sentences describing the game"}\n\n'
        f"Genres — use only these exact values (pick all that apply): {_GENRE_LIST}\n"
        "Platforms — use only these exact values: PC, Switch, PS5, Xbox, Steam Deck\n"
        "Status: 'Unreleased' if not yet released, 'Out' if already out."
    )
    raw = await llm.complete([{"role": "user", "content": prompt}], max_tokens=400)
    result = safe_json_loads(strip_fences(raw)) or {"name": title}
    result["genres"] = mapping.map_genres(result.get("genres") or [])
    result["platforms"] = mapping.map_platforms(result.get("platforms") or [])
    # Groq is never trusted for a precise day — demote its date to approximate text.
    result["release_human"] = clean_field(result.get("release_date", ""))
    result["release_date"] = ""
    result["release_ts"] = None
    result["store_url"] = ""
    return result
