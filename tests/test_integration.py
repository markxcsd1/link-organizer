"""Integration tests — real handler code with every HTTP call mocked by respx."""
import httpx
import pytest
import respx

# conftest stubs env vars before these imports
from game_bot import metadata, steam, discovery

pytestmark = pytest.mark.asyncio


# ── metadata.fetch_page_meta ──────────────────────────────────────────────────

class TestFetchPageMeta:
    @respx.mock
    async def test_youtube_oembed_fast_path(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        respx.get("https://www.youtube.com/oembed").mock(return_value=httpx.Response(
            200, json={"title": "Never Gonna Give You Up", "author_name": "Rick Astley"}))
        respx.get(url).mock(return_value=httpx.Response(200, text="<html></html>"))
        meta = await metadata.fetch_page_meta(url)
        assert meta["title"] == "Never Gonna Give You Up"
        assert meta["author"] == "Rick Astley"

    @respx.mock
    async def test_opengraph_fallback(self):
        url = "https://example.com/article"
        html = ('<html><head><meta property="og:title" content="Great Title"/>'
                '<meta property="og:description" content="A summary."/></head></html>')
        respx.get(url).mock(return_value=httpx.Response(200, text=html))
        meta = await metadata.fetch_page_meta(url)
        assert meta["title"] == "Great Title"
        assert meta["desc"] == "A summary."

    @respx.mock
    async def test_jina_fallback_on_generic_title(self):
        """A JS-heavy host with a shell title must be re-read through Jina."""
        url = "https://www.xbox.com/en-US/games/store/forza/9XYZ"
        respx.get(url).mock(return_value=httpx.Response(
            200, text="<html><head><title>Xbox Official Site | Xbox</title></head></html>"))
        respx.route(method="GET", url__regex=r"^https://r\.jina\.ai/").mock(
            return_value=httpx.Response(200, json={"data": {
                "title": "Forza Horizon 6", "description": "Race across Japan.", "content": ""}}))
        meta = await metadata.fetch_page_meta(url)
        assert meta["title"] == "Forza Horizon 6"


class TestJinaRead:
    @respx.mock
    async def test_parses_json_data(self):
        respx.route(method="GET", url__regex=r"^https://r\.jina\.ai/").mock(
            return_value=httpx.Response(200, json={"data": {
                "title": "Hades II", "description": "A rogue-like.",
                "content": "Release Date: 6 May, 2024"}}))
        meta = await metadata.jina_read("https://example.com/game")
        assert meta["title"] == "Hades II"
        assert "Release Date" in meta["content"]

    @respx.mock
    async def test_failure_returns_empty(self):
        respx.route(method="GET", url__regex=r"^https://r\.jina\.ai/").mock(
            return_value=httpx.Response(500))
        assert await metadata.jina_read("https://example.com/x") == {}


# ── steam ─────────────────────────────────────────────────────────────────────

class TestSteamAppdetails:
    @respx.mock
    async def test_parses_app_data(self):
        payload = {"3714420": {"success": True, "data": {
            "type": "game", "name": "Delta", "developers": ["0xc3pti0n"],
            "genres": [{"description": "Action"}, {"description": "Indie"}, {"description": "Racing"}],
            "release_date": {"coming_soon": True, "date": "2026"},
            "short_description": "A first-person movement platformer about speedrunning.",
            "platforms": {"windows": True, "mac": False, "linux": True},
        }}}
        respx.get(url__regex=r"store\.steampowered\.com/api/appdetails").mock(
            return_value=httpx.Response(200, json=payload))
        out = await steam.appdetails("3714420")
        assert out["name"] == "Delta"
        assert out["developer"] == "0xc3pti0n"
        assert out["platforms"] == ["PC"]
        assert "Racing" in out["genres"]
        assert "Platformer" in out["genres"]          # supplemented from the description
        assert out["coming_soon"] is True
        assert out["release_date"] == "" and out["release_human"] == "2026"
        assert out["store_url"] == "https://store.steampowered.com/app/3714420/"

    @respx.mock
    async def test_unsuccessful_returns_empty(self):
        respx.get(url__regex=r"store\.steampowered\.com/api/appdetails").mock(
            return_value=httpx.Response(200, json={"999": {"success": False}}))
        assert await steam.appdetails("999") == {}


class TestSteamSearchAppid:
    @respx.mock
    async def test_prefers_exact_over_playtest(self):
        # Steam ranks the Playtest and an unrelated game above the real one.
        respx.get(url__regex=r"steamcommunity\.com/actions/SearchApps/").mock(
            return_value=httpx.Response(200, json=[
                {"appid": "4658140", "name": "over the hill Playtest"},
                {"appid": "396860", "name": "Over The Hills And Far Away"},
                {"appid": "2929250", "name": "over the hill"},
            ]))
        assert await steam.search_appid("over the hill") == "2929250"

    @respx.mock
    async def test_no_match_returns_empty(self):
        respx.get(url__regex=r"steamcommunity\.com/actions/SearchApps/").mock(
            return_value=httpx.Response(200, json=[{"appid": "1", "name": "Unrelated Title"}]))
        assert await steam.search_appid("over the hill") == ""


# ── discovery ─────────────────────────────────────────────────────────────────

class TestFindStoreUrl:
    @respx.mock
    async def test_steam_api_exact_match(self):
        respx.get(url__regex=r"steamcommunity\.com/actions/SearchApps/").mock(
            return_value=httpx.Response(200, json=[{"appid": "2483190", "name": "Forza Horizon 6"}]))
        assert await discovery.find_store_url("Forza Horizon 6") == \
            "https://store.steampowered.com/app/2483190/"

    @respx.mock
    async def test_falls_back_to_ddg(self):
        respx.get(url__regex=r"steamcommunity\.com/actions/SearchApps/").mock(
            return_value=httpx.Response(200, json=[{"appid": "1", "name": "Different Game"}]))
        respx.get(url__regex=r"html\.duckduckgo\.com/html").mock(
            return_value=httpx.Response(200, text="<html>no results</html>"))
        assert await discovery.find_store_url("Forza Horizon 6") == ""


class TestFindReview:
    @respx.mock
    async def test_prefers_metacritic_when_it_resolves(self):
        respx.get(url__regex=r"metacritic\.com/game/").mock(
            return_value=httpx.Response(200, text="<html>reviews</html>"))
        assert await discovery.find_review("Forza Horizon 6") == \
            "https://www.metacritic.com/game/forza-horizon-6/"

    @respx.mock
    async def test_falls_back_to_ddg_when_metacritic_404s(self):
        respx.get(url__regex=r"metacritic\.com/game/").mock(return_value=httpx.Response(404))
        html = ('<a href="/l/?uddg=https%3A%2F%2Fwww.gamespot.com%2Freviews%2F'
                'some-review%2F1900-1%2F">GameSpot</a>')
        respx.get(url__regex=r"html\.duckduckgo\.com/html").mock(
            return_value=httpx.Response(200, text=html))
        out = await discovery.find_review("Some Obscure Game")
        assert "gamespot.com" in out


class TestFindTrailer:
    @respx.mock
    async def test_returns_youtube_link(self):
        html = ('<a href="/l/?uddg=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DGzKT3pIkVmo">'
                'trailer</a>')
        respx.get(url__regex=r"html\.duckduckgo\.com/html").mock(
            return_value=httpx.Response(200, text=html))
        out = await discovery.find_trailer("over the hill")
        assert "youtube.com/watch" in out
