"""
Read a shared link into `{title, desc}`.

Order of attempts: YouTube oEmbed (reliable, handles Shorts) → generic oEmbed
auto-discovery → Open Graph tags → the plain <title>/description. If the result
still looks like a JS-rendered shell (store/social pages), re-read it through
Jina Reader, which renders client-side content server-side.
"""
from __future__ import annotations
import re
import httpx
from urllib.parse import urlparse

from .config import JINA_API_KEY

# Page titles that are site headers/boilerplate rather than the content name.
_GENERIC_TITLE_HINTS = (
    "official site", "official website", "| xbox", "store | xbox",
    "playstation store", "nintendo - official", "nintendo store",
    "epic games store", "- microsoft store", "buy games",
)

# Hosts that render content client-side, so a plain fetch returns a shell page.
_JS_HEAVY_HOSTS = ("xbox.com", "nintendo.com", "playstation.com", "instagram.com", "tiktok.com")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def is_generic_title(title: str) -> bool:
    """True when a title looks like a site header rather than the actual content name."""
    if not title or len(title) > 100:
        return True
    tl = title.lower()
    return any(h in tl for h in _GENERIC_TITLE_HINTS)


async def jina_read(url: str) -> dict:
    """
    Render a JS-heavy page through Jina Reader (r.jina.ai) → {title, desc, content}.
    Works with no key (an optional JINA_API_KEY raises the rate limit). Returns {}
    on any failure so the caller keeps its fast-path data.
    """
    headers = {"Accept": "application/json"}
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(f"https://r.jina.ai/{url}", headers=headers)
        if r.status_code != 200:
            return {}
        data = r.json().get("data", {}) or {}
        return {
            "title":   (data.get("title") or "")[:200],
            "desc":    (data.get("description") or "")[:600],
            "content": (data.get("content") or "")[:4000],
        }
    except Exception:
        return {}


async def fetch_page_meta(url: str) -> dict:
    """Return {title, desc, author, final_url} for a shared link."""
    title, desc, author, final_url, html = "", "", "", url, ""

    # Fast path: YouTube direct oEmbed (page HTML is unreliable due to consent screens).
    if re.search(r"(youtube\.com|youtu\.be)", url, re.IGNORECASE):
        yt = await _youtube_oembed(url)
        if yt:
            return yt

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": _UA}) as client:
            r = await client.get(url)
        final_url, html = str(r.url), r.text
    except Exception:
        return {"title": "", "desc": "", "author": "", "final_url": url}

    # 1. oEmbed auto-discovery — works for any platform that embeds a <link> tag.
    m = (re.search(r'<link[^>]+type=["\']application/json\+oembed["\'][^>]+href=["\']([^"\']+)["\']', html, re.IGNORECASE)
         or re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+type=["\']application/json\+oembed["\']', html, re.IGNORECASE))
    if m:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                oe = await client.get(m.group(1))
            if oe.status_code == 200:
                od = oe.json()
                title = od.get("title", "")[:200]
                author = od.get("author_name", "")
                desc = od.get("description", "")[:600] or (f"By {author}" if author else "")
        except Exception:
            pass

    if "youtube" in final_url and not desc:
        desc = _youtube_short_description(html)

    # 2. Open Graph tags, then 3. the plain <title>/description.
    if not title:
        title = _og(html, "title") or _tag_title(html)
    if not desc:
        desc = _og(html, "description") or _meta_description(html)

    # 3.5 JS-render fallback for shell pages (store/social).
    host = (urlparse(final_url).hostname or "").lower()
    if is_generic_title(title) or any(h in host for h in _JS_HEAVY_HOSTS):
        jina = await jina_read(final_url)
        if jina.get("title") and not is_generic_title(jina["title"]):
            title = jina["title"]
        if jina.get("desc") and not desc:
            desc = jina["desc"]

    # 4. Universal fallback: if the title is still just a platform name, try noembed.
    if title.lower().strip() in _PLATFORM_TITLES:
        title, author, desc = await _noembed(url, title, author, desc)

    return {"title": title[:200] if title else "", "desc": desc[:600] if desc else "",
            "author": author, "final_url": final_url}


# ── internal helpers ──────────────────────────────────────────────────────────

_PLATFORM_TITLES = {"youtube", "instagram", "facebook", "tiktok", "twitter", "x", ""}


async def _youtube_oembed(url: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            oe = await client.get(f"https://www.youtube.com/oembed?url={url}&format=json")
        if oe.status_code != 200:
            return None
        od = oe.json()
        title, author = od.get("title", "")[:200], od.get("author_name", "")
        if not title:
            return None
        desc = ""
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                         headers={"User-Agent": _UA}) as client:
                rp = await client.get(url)
            desc = _youtube_short_description(rp.text)
        except Exception:
            pass
        return {"title": title, "desc": desc or f"YouTube video by {author}",
                "author": author, "final_url": url}
    except Exception:
        return None


def _youtube_short_description(html: str) -> str:
    m = re.search(r'"shortDescription":"((?:[^"\\]|\\.){0,1500})"', html)
    return m.group(1).replace("\\n", "\n").replace('\\"', '"')[:800] if m else ""


def _og(html: str, prop: str) -> str:
    pat1 = rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)["\']'
    pat2 = rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:{prop}["\']'
    m = re.search(pat1, html, re.IGNORECASE) or re.search(pat2, html, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _tag_title(html: str) -> str:
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return m.group(1).strip()[:200] if m else ""


def _meta_description(html: str) -> str:
    for pat in (r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']'):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:400]
    return ""


async def _noembed(url: str, title: str, author: str, desc: str) -> tuple[str, str, str]:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            ne = await client.get(f"https://noembed.com/embed?url={url}")
        if ne.status_code == 200:
            nd = ne.json()
            if nd.get("title") and nd["title"].lower() not in _PLATFORM_TITLES:
                return (nd.get("title", title)[:200], nd.get("author_name", author),
                        nd.get("description", desc) or desc)
    except Exception:
        pass
    return title, author, desc
