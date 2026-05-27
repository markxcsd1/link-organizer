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
    _map_type,
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
