"""
Normalisation of noisy source data into the closed vocabularies the Notion "To
Play" database expects, plus URL/title detectors. All functions here are pure.

The Genre/Platform mappers use longest-key-first substring matching so that a
specific term wins over a generic one (e.g. "rogue-lite" beats "rogue", "steam
deck" beats "steam").
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

# ── Genres ────────────────────────────────────────────────────────────────────

_GENRE_MAP: dict[str, str] = {
    # Roguelite / Roguelike
    "roguelite": "Roguelite", "rogue-lite": "Roguelite",
    "roguelike": "Roguelike", "rogue-like": "Roguelike",
    # Deckbuilder
    "deckbuilder": "Deckbuilder", "deck builder": "Deckbuilder",
    "deck-builder": "Deckbuilder", "deckbuilding": "Deckbuilder",
    # Metroidvania
    "metroidvania": "Metroidvania",
    # Soulslike (longer keys win, so this beats "action" via length sort)
    "soulslike": "Soulslike", "souls-like": "Soulslike", "souls like": "Soulslike",
    "soulsborne": "Soulslike",
    # Action (IGDB "Hack and slash/Beat 'em up", "Arcade")
    "action-adventure": "Action", "action adventure": "Action", "action": "Action",
    "hack and slash": "Action", "beat 'em up": "Action", "beat em up": "Action",
    "arcade": "Action",
    # Shooter (IGDB "Shooter"; FPS/TPS)
    "first-person shooter": "Shooter", "first person shooter": "Shooter",
    "third-person shooter": "Shooter", "third person shooter": "Shooter",
    "shooter": "Shooter", "fps": "Shooter",
    # Adventure (IGDB "Adventure", "Point-and-click")
    "point-and-click": "Adventure", "point and click": "Adventure", "adventure": "Adventure",
    # Platformer (IGDB "Platform")
    "platformer": "Platformer", "platform": "Platformer",
    # Survivors-like
    "survivors-like": "Survivors-like", "survivors like": "Survivors-like",
    "survivor": "Survivors-like", "bullet heaven": "Survivors-like",
    # Horror / Survival
    "survival horror": "Horror", "horror": "Horror", "survival": "Survival",
    # Stealth
    "stealth": "Stealth",
    # Racing
    "racing": "Racing", "racer": "Racing",
    # Sports
    "sports": "Sports", "sport": "Sports",
    # Fighting
    "fighting": "Fighting", "fighter": "Fighting",
    # Simulation (IGDB "Simulator"; management/tycoon)
    "simulation": "Simulation", "simulator": "Simulation",
    "management": "Simulation", "tycoon": "Simulation",
    # Puzzle
    "puzzle": "Puzzle",
    # Strategy (IGDB "Real Time Strategy (RTS)", "Turn-based strategy (TBS)", "Tactical")
    "real time strategy": "Strategy", "turn-based strategy": "Strategy",
    "turn-based": "Strategy", "tactical": "Strategy", "tactics": "Strategy",
    "strategy": "Strategy", "rts": "Strategy",
    # Tower Defense
    "tower defense": "Tower Defense", "tower defence": "Tower Defense",
    # City Builder
    "city builder": "City Builder", "city-builder": "City Builder", "citybuilder": "City Builder",
    # Open World / Sandbox
    "open world": "Open World", "open-world": "Open World", "sandbox": "Sandbox",
    # MMO (longest-first keeps "mmorpg" off RPG)
    "massively multiplayer": "MMO", "mmorpg": "MMO", "mmo": "MMO",
    # RPG (IGDB "Role-playing (RPG)")
    "role-playing": "RPG", "role playing": "RPG", "rpg": "RPG",
    # MOBA / Battle Royale
    "moba": "MOBA", "battle royale": "Battle Royale",
    # Visual Novel
    "visual novel": "Visual Novel",
    # Rhythm (IGDB "Music")
    "rhythm": "Rhythm", "music": "Rhythm",
}

_VALID_GENRES = frozenset(_GENRE_MAP.values())

# Distinctive subgenres Steam's coarse genre tags miss — safe to read from a
# description because they're unambiguous as whole phrases.
DESC_GENRE_HINTS: dict[str, str] = {
    "platformer": "Platformer", "metroidvania": "Metroidvania",
    "roguelike": "Roguelike", "rogue-like": "Roguelike",
    "roguelite": "Roguelite", "rogue-lite": "Roguelite",
    "soulslike": "Soulslike", "souls-like": "Soulslike",
    "deckbuilder": "Deckbuilder", "deck-builder": "Deckbuilder", "deck builder": "Deckbuilder",
    "visual novel": "Visual Novel", "tower defense": "Tower Defense",
    "battle royale": "Battle Royale", "open world": "Open World", "open-world": "Open World",
}


def map_genres(raw: list) -> list:
    """Map raw genre strings to the DB's valid multi-select options (deduplicated)."""
    return _map(raw, _VALID_GENRES, _GENRE_MAP)


# ── Platforms ─────────────────────────────────────────────────────────────────

_PLATFORM_MAP: dict[str, str] = {
    "pc (microsoft windows)": "PC", "microsoft windows": "PC",
    "steam deck": "Steam Deck",
    "nintendo switch": "Switch",
    "playstation 5": "PS5", "ps5": "PS5",
    "xbox series x|s": "Xbox", "xbox series x": "Xbox", "xbox series s": "Xbox",
    "xbox series": "Xbox",
    "pc": "PC", "windows": "PC", "mac": "PC", "linux": "PC", "steam": "PC",
    "switch": "Switch", "nintendo": "Switch",
    "playstation": "PS5",
    "xbox": "Xbox",
}

_VALID_PLATFORMS = frozenset({"PC", "Switch", "PS5", "Xbox", "Steam Deck"})


def map_platforms(raw: list) -> list:
    """Map raw platform strings to the DB's valid multi-select options (deduplicated)."""
    return _map(raw, _VALID_PLATFORMS, _PLATFORM_MAP)


def _map(raw: list, valid: frozenset, table: dict[str, str]) -> list:
    result: list = []
    seen: set = set()
    for item in raw:
        s = str(item).strip()
        if s in valid and s not in seen:                 # already a valid option
            result.append(s); seen.add(s); continue
        lower = s.lower()
        for key in sorted(table, key=len, reverse=True):  # longest key wins
            if key in lower:
                v = table[key]
                if v not in seen:
                    result.append(v); seen.add(v)
                break
    return result


# ── Release dates ─────────────────────────────────────────────────────────────

_DATE_FORMATS = (
    "%d %b %Y", "%d %B %Y", "%d %b, %Y", "%d %B, %Y",
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    "%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y",
)


def parse_exact_date(raw: str) -> str:
    """
    Return ISO 'YYYY-MM-DD' only if `raw` is a full day-month-year date, else ''.
    A year ("2026"), month ("Jun 2026"), or quarter ("Q1 2026") yields '' — we
    never fabricate a precise day from an approximate one.
    """
    raw = (raw or "").strip().rstrip(".,")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def extract_store_release_date(content: str) -> tuple[str, str]:
    """
    Pull a store page's stated release date out of Jina markdown. Returns
    (exact_iso, human): exact_iso is set only for a full day; otherwise human
    holds the raw text ("Coming soon", "Q1 2025", "2025").
    """
    if not content:
        return "", ""
    # Handles "Release Date: 6 May 2024", "**Release Date:** ...", and table rows
    # like "| Release Date | May 12, 2023 |".
    m = re.search(r"Release\s*Date[\s:|]+\**\s*([^\n|*<]{3,40})", content, re.IGNORECASE)
    if not m:
        return "", ""
    raw = m.group(1).strip().rstrip(".,")
    return parse_exact_date(raw), raw


# ── Titles & URLs ─────────────────────────────────────────────────────────────

def clean_game_title(title: str) -> str:
    """
    Strip trailer/gameplay/store cruft from a page or video title to recover the
    game name — e.g. 'Hades II - Official Launch Trailer' -> 'Hades II',
    'The Last Salvage Squad Trailer' -> 'The Last Salvage Squad'.
    """
    t = title or ""
    # Everything after a separator that introduces trailer/store boilerplate.
    t = re.sub(
        r"\s*[-|:–—]\s*(official\s+)?[^-|:–—]*?"
        r"(trailer|gameplay|teaser|reveal|announce\w*|launch|review|walkthrough|dlc|"
        r"on steam|steam|pc game|out now|available now).*$",
        "", t, flags=re.IGNORECASE)
    # Trailing trailer/gameplay keywords even without a separator.
    t = re.sub(r"\s+(official\s+)?(launch|reveal|announcement|cinematic|gameplay|teaser)?\s*trailer\b.*$",
               "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+gameplay\b.*$", "", t, flags=re.IGNORECASE)
    # Outlet suffixes ("| IGN", "- GameSpot").
    t = re.sub(r"\s*[\|\-–—]\s*(IGN|GameSpot|Game Informer|Gamescom|PlayStation|Xbox|Nintendo)\b.*$",
               "", t, flags=re.IGNORECASE)
    return t.strip(" -|:–—")


def game_name_from_url(url: str) -> str:
    """Recover a probable game name from a store URL's path slug."""
    path = urlparse(url).path
    patterns = (
        (r"/app/\d+/([^/?#]+)", "_-"),            # Steam: /app/1234/Game_Name/
        (r"/p/([^/?#]+)/?$", "-"),                # Epic: /p/game-name
        (r"/game/([^/?#]+)/?$", "_-"),            # GOG: /game/game_name
        (r"/games/store/([^/?#]+)", "-"),         # Xbox: /games/store/game-name/...
        (r"/store/(?:products|items)/([^/?#]+)", "-"),  # Nintendo
    )
    for pattern, seps in patterns:
        m = re.search(pattern, path)
        if m:
            name = m.group(1)
            for sep in seps:
                name = name.replace(sep, " ")
            return name.strip()
    return ""


_GAME_URL_RE = re.compile(
    r"(store\.steampowered\.com|gog\.com/game|epicgames\.com|itch\.io"
    r"|nintendo\.com/store|playstation\.com/[a-z-]+/games|xbox\.com/[a-z-]+/games)",
    re.IGNORECASE,
)
_VIDEO_URL_RE = re.compile(r"(youtube\.com/watch|youtu\.be/|vimeo\.com/\d)", re.IGNORECASE)


def is_game_url(url: str) -> bool:
    """True for known game-store URLs (Steam, GOG, Epic, itch, Nintendo, PS, Xbox)."""
    return bool(_GAME_URL_RE.search(url))


def is_video_url(url: str) -> bool:
    """True for YouTube / Vimeo links (typically a trailer)."""
    return bool(_VIDEO_URL_RE.search(url))


def clean_video_url(url: str) -> str:
    """Normalise a YouTube link to just its video id, dropping playlist/index cruft."""
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})", url)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else url
