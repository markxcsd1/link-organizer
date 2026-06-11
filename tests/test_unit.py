"""Unit tests for pure helper functions — no HTTP, no Notion, no Groq."""
import json
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# conftest stubs env vars before this import
from api.index import (
    _extract_json,
    _clean_field,
    _clean_topic_name,
    _detect_addto_intent,
    _map_type,
    _map_game_genres,
    _map_game_platforms,
    _is_game_url,
    parse_command,
    _rich_text,
    NOTION_DB,
    CATEGORY_EMOJI,
)


# ── _extract_json ─────────────────────────────────────────────────────────────

class TestExtractJson:
    def test_bare_json(self):
        raw = '{"key": "val"}'
        assert json.loads(_extract_json(raw)) == {"key": "val"}

    def test_trailing_text(self):
        """Groq often adds text after the JSON object — must be stripped."""
        raw = '{"a": 1}\nHere is the explanation…'
        result = _extract_json(raw)
        assert json.loads(result) == {"a": 1}

    def test_leading_text(self):
        raw = "Sure! Here is the JSON:\n\n{\"x\": 42}"
        assert json.loads(_extract_json(raw)) == {"x": 42}

    def test_nested_objects(self):
        raw = '{"outer": {"inner": true}, "list": [1,2]}'
        assert json.loads(_extract_json(raw)) == {"outer": {"inner": True}, "list": [1, 2]}

    def test_braces_inside_string(self):
        """Braces inside quoted strings must not confuse the parser."""
        raw = '{"text": "has {curly} braces"} trailing'
        assert json.loads(_extract_json(raw)) == {"text": "has {curly} braces"}

    def test_escaped_quote_in_string(self):
        raw = '{"q": "say \\"hello\\""} extra'
        assert json.loads(_extract_json(raw)) == {"q": 'say "hello"'}

    def test_no_json_returns_original(self):
        raw = "no json here"
        assert _extract_json(raw) == raw

    def test_code_fence_prefix(self):
        """Simulate Groq wrapping output in ```json ... ```"""
        raw = '```json\n{"cat": "location"}\n```'
        # Strip fences first (as done in the handler), then extract
        import re
        stripped = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.IGNORECASE)
        stripped = re.sub(r"```$", "", stripped.strip())
        assert json.loads(_extract_json(stripped)) == {"cat": "location"}


# ── _clean_field ──────────────────────────────────────────────────────────────

class TestCleanField:
    @pytest.mark.parametrize("bad", [
        "Not found",
        "not found in the given URL",
        "Not specified",
        "not available",
        "N/A",
        "unknown",
        "Not Available",
        "UNKNOWN",
    ])
    def test_strips_placeholders(self, bad):
        assert _clean_field(bad) == ""

    @pytest.mark.parametrize("good", [
        "Sifnos, Greece",
        "4.5/5",
        "Beach bar",
        "Wine bar in Apollonia",
        "Kastro Village",
    ])
    def test_keeps_real_values(self, good):
        assert _clean_field(good) == good

    def test_empty_string(self):
        assert _clean_field("") == ""

    def test_none_like_falsy(self):
        # The function accepts str; falsy values should return ""
        assert _clean_field(None) == ""  # type: ignore[arg-type]


# ── _map_type ─────────────────────────────────────────────────────────────────

class TestMapType:
    @pytest.mark.parametrize("raw,expected", [
        ("Restaurant",   "Restaurant"),
        ("Bar",          "Bar"),
        ("Beach",        "Beach"),
        ("Sight",        "Sight"),
        ("Other",        "Other"),
    ])
    def test_exact_valid_types(self, raw, expected):
        assert _map_type(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("restaurant",   "Restaurant"),
        ("taverna",      "Restaurant"),
        ("tavern",       "Restaurant"),
        ("beach bar",    "Bar"),
        ("wine bar",     "Bar"),
        ("cafe",         "Cafe"),
        ("coffee shop",  "Cafe"),
        ("monastery",    "Sight"),
        ("church",       "Sight"),
        ("hotel",        "Hotel"),
        ("villa",        "Hotel"),
        ("village",      "Village"),
        ("museum",       "Museum"),
        ("shop",         "Shop"),
    ])
    def test_normalises_raw_strings(self, raw, expected):
        assert _map_type(raw) == expected

    def test_unknown_returns_other(self):
        assert _map_type("some weird venue") == "Other"

    def test_empty_returns_empty(self):
        assert _map_type("") == ""


# ── parse_command ─────────────────────────────────────────────────────────────

class TestParseCommand:
    def test_valid_category_with_note(self):
        cat, note = parse_command("!video great tutorial")
        assert cat == "video"
        assert note == "great tutorial"

    def test_valid_category_no_note(self):
        cat, note = parse_command("!recipe")
        assert cat == "recipe"
        assert note == ""

    def test_all_valid_categories(self):
        for cat in NOTION_DB:
            parsed_cat, _ = parse_command(f"!{cat} something")
            assert parsed_cat == cat

    def test_no_command(self):
        cat, note = parse_command("just a regular note")
        assert cat is None
        assert note == "just a regular note"

    def test_invalid_category_treated_as_note(self):
        cat, note = parse_command("!notacategory something")
        assert cat is None

    def test_whitespace_stripped(self):
        cat, note = parse_command("  !article   some title  ")
        assert cat == "article"
        assert note == "some title"


# ── _rich_text ────────────────────────────────────────────────────────────────

class TestRichText:
    def test_basic(self):
        rt = _rich_text("hello")
        assert rt == [{"text": {"content": "hello"}}]

    def test_truncated_at_2000(self):
        long = "x" * 3000
        rt = _rich_text(long)
        assert len(rt[0]["text"]["content"]) == 2000


# ── _clean_topic_name ─────────────────────────────────────────────────────────

class TestCleanTopicName:
    @pytest.mark.parametrize("raw,expected", [
        ("  TOKYO 🗼 ",       "Tokyo"),
        ("tokyo",             "Tokyo"),
        ("Tokyo",             "Tokyo"),
        ("  new   york  ",    "New York"),
        ("NYC",               "NYC"),         # short ALL-CAPS preserved
        ("USA",               "USA"),
        ("LA",                "LA"),
        ("✈️ japan trip 🇯🇵", "Japan Trip"),
        ("",                  ""),
        ("   ",               ""),
    ])
    def test_normalises(self, raw, expected):
        assert _clean_topic_name(raw) == expected


# ── _detect_addto_intent ──────────────────────────────────────────────────────

class TestDetectAddtoIntent:
    @pytest.mark.parametrize("text,topic", [
        ("Save it in Sifnos",            "Sifnos"),
        ("save in Tokyo",                "Tokyo"),
        ("add to Amorgos",               "Amorgos"),
        ("Add it to Amorgos",            "Amorgos"),
        ("save to my Sifnos list",       "Sifnos"),
        ("Save to the Tokyo page",       "Tokyo"),
        ("put it under Japan trip",      "Japan"),
        ("stash this in the Bucket list","Bucket"),
        ("store it in Athens",           "Athens"),
        ("drop this into Concerts",      "Concerts"),
    ])
    def test_extracts_topic(self, text, topic):
        assert _detect_addto_intent(text) == topic

    @pytest.mark.parametrize("text", [
        "Save",
        "save it",
        "ok save it",                  # doesn't start with verb
        "cancel",
        "yes",
        "change category to video",
        "Sifnos",                      # no verb at all
        "",
    ])
    def test_returns_none(self, text):
        assert _detect_addto_intent(text) is None


# ── _map_type new types ───────────────────────────────────────────────────────

class TestMapTypeNewTypes:
    @pytest.mark.parametrize("raw,expected", [
        ("Event",      "Event"),
        ("Festival",   "Festival"),
        ("Activity",   "Activity"),
        ("Place",      "Place"),
        ("concert",    "Event"),
        ("hike",       "Activity"),
        ("tour",       "Activity"),
    ])
    def test_new_types(self, raw, expected):
        assert _map_type(raw) == expected


# ── CATEGORY_EMOJI coverage ───────────────────────────────────────────────────

def test_all_categories_have_emoji():
    for cat in NOTION_DB:
        assert cat in CATEGORY_EMOJI, f"Missing emoji for category: {cat}"


# ── _map_game_genres ──────────────────────────────────────────────────────────

class TestMapGameGenres:
    @pytest.mark.parametrize("raw,expected", [
        (["Action"],                  ["Action"]),
        (["RPG"],                     ["RPG"]),
        (["Roguelite"],               ["Roguelite"]),
        (["roguelike"],               ["Roguelike"]),
        (["deckbuilder"],             ["Deckbuilder"]),
        (["deck builder"],            ["Deckbuilder"]),
        (["metroidvania"],            ["Metroidvania"]),
        (["platformer"],              ["Platformer"]),
        (["survivors-like"],          ["Survivors-like"]),
        (["survivor"],                ["Survivors-like"]),
        (["strategy"],                ["Strategy"]),
        (["role-playing"],            ["RPG"]),
        (["Action", "RPG"],           ["Action", "RPG"]),
        (["unknown genre"],           []),
        ([],                          []),
        (["Action", "action"],        ["Action"]),    # dedup
    ])
    def test_mapping(self, raw, expected):
        assert _map_game_genres(raw) == expected


# ── _map_game_platforms ───────────────────────────────────────────────────────

class TestMapGamePlatforms:
    @pytest.mark.parametrize("raw,expected", [
        (["PC"],                      ["PC"]),
        (["pc"],                      ["PC"]),
        (["windows"],                 ["PC"]),
        (["steam"],                   ["PC"]),
        (["Steam Deck"],              ["Steam Deck"]),
        (["steam deck"],              ["Steam Deck"]),
        (["Switch"],                  ["Switch"]),
        (["nintendo switch"],         ["Switch"]),
        (["PS5"],                     ["PS5"]),
        (["playstation 5"],           ["PS5"]),
        (["Xbox"],                    ["Xbox"]),
        (["xbox series"],             ["Xbox"]),
        (["PC", "Switch", "PS5"],     ["PC", "Switch", "PS5"]),
        (["unknown platform"],        []),
        ([],                          []),
        # single string containing "steam deck" must prefer Steam Deck over PC
        (["steam deck"],              ["Steam Deck"]),
    ])
    def test_mapping(self, raw, expected):
        assert _map_game_platforms(raw) == expected


# ── IGDB-specific name mapping ────────────────────────────────────────────────

class TestIgdbGenreNames:
    """IGDB returns genre names like 'Role-playing (RPG)' — must map correctly."""
    @pytest.mark.parametrize("raw,expected", [
        (["Role-playing (RPG)"],              ["RPG"]),
        (["Platform"],                        ["Platformer"]),
        (["Hack and slash/Beat 'em up"],      ["Action"]),
        (["Shooter"],                         ["Action"]),
        (["Real Time Strategy (RTS)"],        ["Strategy"]),
        (["Turn-based strategy (TBS)"],       ["Strategy"]),
        (["Metroidvania"],                    ["Metroidvania"]),
        (["Indie"],                           []),   # no mapping
        (["Role-playing (RPG)", "Strategy"],  ["RPG", "Strategy"]),
    ])
    def test_igdb_genre(self, raw, expected):
        assert _map_game_genres(raw) == expected


class TestIgdbPlatformNames:
    """IGDB returns platform names like 'PC (Microsoft Windows)' — must map correctly."""
    @pytest.mark.parametrize("raw,expected", [
        (["PC (Microsoft Windows)"],          ["PC"]),
        (["Nintendo Switch"],                 ["Switch"]),
        (["PlayStation 5"],                   ["PS5"]),
        (["Xbox Series X|S"],                 ["Xbox"]),
        (["Steam Deck"],                      ["Steam Deck"]),
        (["Mac"],                             ["PC"]),
        (["Linux"],                           ["PC"]),
        (["PC (Microsoft Windows)", "Mac"],   ["PC"]),   # dedup to single PC
    ])
    def test_igdb_platform(self, raw, expected):
        assert _map_game_platforms(raw) == expected


# ── _is_game_url ──────────────────────────────────────────────────────────────

class TestIsGameUrl:
    @pytest.mark.parametrize("url", [
        "https://store.steampowered.com/app/1456480/Hades_II/",
        "https://www.gog.com/game/disco_elysium",
        "https://store.epicgames.com/en-US/p/hades-2",
        "https://itch.io/games",
        "https://www.nintendo.com/store/products/hollow-knight",
    ])
    def test_game_urls(self, url):
        assert _is_game_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.amazon.com/dp/B08N5WRWNW",
        "https://maps.google.com/place/foo",
        "https://github.com/anthropics/claude-code",
    ])
    def test_non_game_urls(self, url):
        assert _is_game_url(url) is False
