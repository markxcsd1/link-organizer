"""Unit tests for the pure helpers — no HTTP, no Notion, no Groq."""
import json
from datetime import datetime, timezone

import pytest

# conftest stubs env vars before these imports
from game_bot import text_utils, mapping, metadata, igdb, steam, discovery


# ── text_utils ────────────────────────────────────────────────────────────────

class TestExtractJson:
    def test_bare_json(self):
        assert json.loads(text_utils.extract_json('{"key": "val"}')) == {"key": "val"}

    def test_trailing_text(self):
        assert json.loads(text_utils.extract_json('{"a": 1}\nexplanation…')) == {"a": 1}

    def test_leading_text(self):
        assert json.loads(text_utils.extract_json('Sure:\n\n{"x": 42}')) == {"x": 42}

    def test_braces_inside_string(self):
        assert json.loads(text_utils.extract_json('{"t": "has {curly}"} trailing')) == {"t": "has {curly}"}

    def test_escaped_quote(self):
        assert json.loads(text_utils.extract_json('{"q": "say \\"hi\\""} extra')) == {"q": 'say "hi"'}

    def test_no_json_returns_original(self):
        assert text_utils.extract_json("no json here") == "no json here"


class TestCleanField:
    @pytest.mark.parametrize("bad", ["Not found", "Not specified", "N/A", "unknown", "not available"])
    def test_strips_placeholders(self, bad):
        assert text_utils.clean_field(bad) == ""

    @pytest.mark.parametrize("good", ["Playground Games", "2026-05-18", "Racing"])
    def test_keeps_real_values(self, good):
        assert text_utils.clean_field(good) == good

    def test_empty(self):
        assert text_utils.clean_field("") == ""


class TestSafeJsonLoads:
    def test_valid(self):
        assert text_utils.safe_json_loads('{"a": 1}') == {"a": 1}

    def test_prefix_and_suffix(self):
        assert text_utils.safe_json_loads('Sure: {"category": "game"} done') == {"category": "game"}

    def test_trailing_comma_repaired(self):
        assert text_utils.safe_json_loads('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}

    def test_unrecoverable_returns_none(self):
        # Unescaped inner quotes — the failure mode that once crashed a save.
        assert text_utils.safe_json_loads('{"s": "he said "hi" to all"}') is None


class TestLooksLikeGame:
    @pytest.mark.parametrize("text", [
        "Hades II - Official Gameplay Trailer", "Now in Early Access on Steam",
        "A roguelike deckbuilder", "coming to Nintendo Switch",
    ])
    def test_positive(self, text):
        assert text_utils.looks_like_game(text) is True

    @pytest.mark.parametrize("text", ["Best pasta recipe", "How to fix a faucet", ""])
    def test_negative(self, text):
        assert text_utils.looks_like_game(text) is False


class TestRichText:
    def test_basic(self):
        assert text_utils.rich_text("hello") == [{"text": {"content": "hello"}}]

    def test_truncated_at_2000(self):
        assert len(text_utils.rich_text("x" * 3000)[0]["text"]["content"]) == 2000


# ── mapping: genres ───────────────────────────────────────────────────────────

class TestMapGenres:
    @pytest.mark.parametrize("raw,expected", [
        (["Action"], ["Action"]),
        (["roguelike"], ["Roguelike"]),
        (["deck builder"], ["Deckbuilder"]),
        (["survivor"], ["Survivors-like"]),
        (["Action", "action"], ["Action"]),        # dedup
        (["unknown genre"], []),
        ([], []),
        # IGDB-style names
        (["Role-playing (RPG)"], ["RPG"]),
        (["Platform"], ["Platformer"]),
        (["Shooter"], ["Shooter"]),                 # its own genre, not Action
        (["Hack and slash/Beat 'em up"], ["Action"]),
        (["Real Time Strategy (RTS)"], ["Strategy"]),
        (["Sport"], ["Sports"]),
        (["Simulator"], ["Simulation"]),
        (["Music"], ["Rhythm"]),
        (["survival horror"], ["Horror"]),
        (["survival"], ["Survival"]),
        (["mmorpg"], ["MMO"]),
        (["Indie"], []),                            # no mapping
        (["Racing", "Arcade", "Sport"], ["Racing", "Action", "Sports"]),
    ])
    def test_mapping(self, raw, expected):
        assert mapping.map_genres(raw) == expected


class TestMapPlatforms:
    @pytest.mark.parametrize("raw,expected", [
        (["PC"], ["PC"]),
        (["windows"], ["PC"]),
        (["steam deck"], ["Steam Deck"]),           # beats "steam" -> PC
        (["Nintendo Switch"], ["Switch"]),
        (["PlayStation 5"], ["PS5"]),
        (["Xbox Series X|S"], ["Xbox"]),
        (["PC (Microsoft Windows)", "Mac"], ["PC"]),  # dedup to one PC
        (["unknown"], []),
    ])
    def test_mapping(self, raw, expected):
        assert mapping.map_platforms(raw) == expected


# ── mapping: titles & URLs ────────────────────────────────────────────────────

class TestCleanGameTitle:
    @pytest.mark.parametrize("raw,expected", [
        ("The Last Salvage Squad Trailer", "The Last Salvage Squad"),
        ("Hades II - Official Launch Trailer", "Hades II"),
        ("Elden Ring Gameplay Reveal | IGN", "Elden Ring"),
        ("Cyberpunk 2077 - Official Cinematic Trailer", "Cyberpunk 2077"),
        # Must NOT over-strip real titles containing a colon/dash
        ("Hollow Knight: Silksong", "Hollow Knight: Silksong"),
        ("The Last of Us Part II", "The Last of Us Part II"),
    ])
    def test_clean(self, raw, expected):
        assert mapping.clean_game_title(raw) == expected


class TestIsGameUrl:
    @pytest.mark.parametrize("url", [
        "https://store.steampowered.com/app/1456480/Hades_II/",
        "https://www.gog.com/game/disco_elysium",
        "https://store.epicgames.com/en-US/p/hades-2",
        "https://www.nintendo.com/store/products/hollow-knight",
    ])
    def test_game_urls(self, url):
        assert mapping.is_game_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://github.com/anthropics/claude-code",
    ])
    def test_non_game_urls(self, url):
        assert mapping.is_game_url(url) is False


class TestCleanVideoUrl:
    @pytest.mark.parametrize("raw,expected", [
        ("https://www.youtube.com/watch?v=GzKT3pIkVmo&list=WL&index=1",
         "https://www.youtube.com/watch?v=GzKT3pIkVmo"),
        ("https://youtu.be/GzKT3pIkVmo?si=abc", "https://www.youtube.com/watch?v=GzKT3pIkVmo"),
    ])
    def test_normalises(self, raw, expected):
        assert mapping.clean_video_url(raw) == expected

    def test_non_youtube_unchanged(self):
        assert mapping.clean_video_url("https://vimeo.com/123") == "https://vimeo.com/123"


class TestGameNameFromUrl:
    @pytest.mark.parametrize("url,expected", [
        ("https://store.steampowered.com/app/1456480/Hades_II/", "Hades II"),
        ("https://www.nintendo.com/store/products/hollow-knight-silksong", "hollow knight silksong"),
        ("https://www.youtube.com/watch?v=abc", ""),
    ])
    def test_extract(self, url, expected):
        assert mapping.game_name_from_url(url) == expected


# ── mapping: dates ────────────────────────────────────────────────────────────

class TestParseExactDate:
    @pytest.mark.parametrize("raw,iso", [
        ("Jun 17, 2026", "2026-06-17"), ("17 Jun 2026", "2026-06-17"),
        ("2026-06-17", "2026-06-17"), ("6 May, 2024", "2024-05-06"),
    ])
    def test_full_dates(self, raw, iso):
        assert mapping.parse_exact_date(raw) == iso

    @pytest.mark.parametrize("raw", ["2026", "Jun 2026", "Q1 2026", "Coming soon", "", "TBA"])
    def test_approximate_returns_empty(self, raw):
        assert mapping.parse_exact_date(raw) == ""


class TestExtractStoreReleaseDate:
    @pytest.mark.parametrize("content,iso,frag", [
        ("Release Date: 6 May, 2024", "2024-05-06", "6 May"),
        ("**Release Date:** 21 Sep 2023", "2023-09-21", "21 Sep"),
        ("| Release Date | May 12, 2023 |", "2023-05-12", "May 12"),
    ])
    def test_exact(self, content, iso, frag):
        d, human = mapping.extract_store_release_date(content)
        assert d == iso and frag in human

    @pytest.mark.parametrize("content,human", [
        ("Release Date: Coming soon", "Coming soon"),
        ("Release Date: Q1 2025", "Q1 2025"),
    ])
    def test_approximate(self, content, human):
        d, h = mapping.extract_store_release_date(content)
        assert d == "" and human in h

    def test_no_label(self):
        assert mapping.extract_store_release_date("random text") == ("", "")


# ── metadata ──────────────────────────────────────────────────────────────────

class TestIsGenericTitle:
    @pytest.mark.parametrize("title", [
        "", "Xbox Official Site: Consoles, Games and Community | Xbox",
        "Nintendo - Official Site", "x" * 101,
    ])
    def test_generic(self, title):
        assert metadata.is_generic_title(title) is True

    @pytest.mark.parametrize("title", ["Hades II", "Elden Ring on Steam", "Hollow Knight: Silksong"])
    def test_real(self, title):
        assert metadata.is_generic_title(title) is False


# ── igdb.select_release ───────────────────────────────────────────────────────

def _ts(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


class TestSelectRelease:
    def test_exact_category0(self):
        ts = _ts(2024, 5, 6)
        game = {"release_dates": [{"category": 0, "date": ts, "region": 8, "human": "May 06, 2024"}]}
        iso, human, out_ts = igdb.select_release(game)
        assert iso == "2024-05-06" and out_ts == ts

    def test_prefers_exact_over_approx(self):
        ts = _ts(2024, 5, 6)
        game = {"release_dates": [
            {"category": 2, "date": ts - 5000, "region": 1, "human": "2024"},
            {"category": 0, "date": ts, "region": 2, "human": "May 06, 2024"},
        ]}
        assert igdb.select_release(game)[0] == "2024-05-06"

    def test_year_only_is_approximate(self):
        ts = _ts(2025, 1, 1)
        game = {"release_dates": [{"category": 2, "date": ts, "region": 8, "human": "2025"}]}
        iso, human, out_ts = igdb.select_release(game)
        assert iso == "" and human == "2025" and out_ts == ts

    def test_fallback_first_release_date(self):
        iso, human, ts = igdb.select_release({"first_release_date": _ts(2024, 5, 6)})
        assert iso == "" and human == "" and ts == _ts(2024, 5, 6)

    def test_empty(self):
        assert igdb.select_release({}) == ("", "", None)


# ── steam / discovery (pure parts) ────────────────────────────────────────────

class TestSteamAppidFromUrl:
    @pytest.mark.parametrize("url,appid", [
        ("https://store.steampowered.com/app/3714420/Delta/", "3714420"),
        ("https://store.steampowered.com/app/2483190/", "2483190"),
        ("https://www.youtube.com/watch?v=abc", ""),
    ])
    def test_extract(self, url, appid):
        assert steam.appid_from_url(url) == appid


class TestMetacriticUrl:
    @pytest.mark.parametrize("name,expected", [
        ("Forza Horizon 6", "https://www.metacritic.com/game/forza-horizon-6/"),
        ("Baldur's Gate 3", "https://www.metacritic.com/game/baldurs-gate-3/"),
        ("The Last of Us Part II", "https://www.metacritic.com/game/the-last-of-us-part-ii/"),
    ])
    def test_slug(self, name, expected):
        assert discovery.metacritic_url(name) == expected

    def test_empty(self):
        assert discovery.metacritic_url("") == ""
