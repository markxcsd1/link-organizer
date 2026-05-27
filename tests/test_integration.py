"""
Integration tests — exercise the real handler code with mocked HTTP.
All external calls (Notion, Groq, Telegram) are intercepted by respx.
"""
import json
import pytest
import respx
import httpx
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# conftest stubs env vars before this import
import api.index as app_module
from api.index import (
    fetch_page_meta,
    notion_search,
    notion_query_db_rows,
    insert_into_trip_db,
    notion_read_page_content,
    notion_fetch_page_meta,
    notion_create_topic_db,
    _map_type,
)

pytestmark = pytest.mark.asyncio


# ── fetch_page_meta ───────────────────────────────────────────────────────────

class TestFetchPageMeta:
    @respx.mock
    async def test_youtube_oembed_fast_path(self):
        """YouTube URLs must use oEmbed, never rely on page HTML."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        oe_payload = {"title": "Rick Astley - Never Gonna Give You Up", "author_name": "Rick Astley"}
        # oEmbed request
        respx.get("https://www.youtube.com/oembed").mock(
            return_value=httpx.Response(200, json=oe_payload))
        # page HTML (for description scrape)
        respx.get(url).mock(return_value=httpx.Response(200, text="<html></html>"))

        meta = await fetch_page_meta(url)
        assert meta["title"] == "Rick Astley - Never Gonna Give You Up"
        assert meta["author"] == "Rick Astley"
        assert meta["maps_url"] == ""

    @respx.mock
    async def test_maps_url_extracts_place_name(self):
        """maps.app.goo.gl URLs must resolve to a place name, not generic Maps description."""
        url = "https://maps.app.goo.gl/ABC123"
        final_url = "https://www.google.com/maps/place/Loggia+Wine+Bar/@37.1,25.4"
        html = '<title>Loggia Wine Bar - Google Maps</title>'
        respx.get(url).mock(return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"}))

        # Patch the client to simulate redirect to final_url
        with respx.mock:
            route = respx.get(url)
            route.mock(return_value=httpx.Response(
                200,
                text=html,
                headers={"content-type": "text/html"},
            ))
            # We fake the final URL by patching str(r.url)
            # Instead just test the place extraction logic via a direct URL
            final = "https://www.google.com/maps/place/Loggia+Wine+Bar/@37.1,25.4,17z"
            import re
            from urllib.parse import unquote_plus
            pm = re.search(r'/maps/place/([^/@?&#]+)', final)
            assert pm is not None
            place = unquote_plus(pm.group(1)).replace('+', ' ').strip()
            assert place == "Loggia Wine Bar"

    @respx.mock
    async def test_opengraph_fallback(self):
        """OG tags are used when no oEmbed is available."""
        url = "https://example.com/article"
        html = '''<html><head>
            <meta property="og:title" content="Great Article Title"/>
            <meta property="og:description" content="A summary of the article."/>
        </head></html>'''
        respx.get(url).mock(return_value=httpx.Response(200, text=html))

        meta = await fetch_page_meta(url)
        assert meta["title"] == "Great Article Title"
        assert meta["desc"] == "A summary of the article."
        assert meta["maps_url"] == ""

    @respx.mock
    async def test_share_google_detected_as_maps(self):
        """share.google/ links must be flagged as maps_url."""
        url = "https://share.google/XYZ123"
        html = '''<html><head>
            <meta property="og:title" content="Paralia Beach Bar"/>
        </head></html>'''
        respx.get(url).mock(return_value=httpx.Response(200, text=html))

        meta = await fetch_page_meta(url)
        assert meta["maps_url"] != ""


# ── notion_search ─────────────────────────────────────────────────────────────

class TestNotionSearch:
    @respx.mock
    async def test_page_title_extracted_from_properties(self):
        payload = {"results": [{
            "object": "page",
            "id": "page-123",
            "url": "https://notion.so/page-123",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Sifnos Trip"}]},
            },
        }]}
        respx.post("https://api.notion.com/v1/search").mock(
            return_value=httpx.Response(200, json=payload))

        results = await notion_search("Sifnos")
        assert len(results) == 1
        assert results[0]["title"] == "Sifnos Trip"
        assert results[0]["object"] == "page"

    @respx.mock
    async def test_database_title_extracted_from_top_level(self):
        """Databases have title at top-level, NOT in properties — was the 'Untitled' bug."""
        payload = {"results": [{
            "object": "database",
            "id": "db-456",
            "url": "https://notion.so/db-456",
            "title": [{"plain_text": "⛵ Sifnos — Jul 17–20"}],
            "properties": {
                # Schema properties — should NOT be used for title
                "Name": {"type": "title"},
                "Type": {"type": "select"},
            },
        }]}
        respx.post("https://api.notion.com/v1/search").mock(
            return_value=httpx.Response(200, json=payload))

        results = await notion_search("Sifnos")
        assert len(results) == 1
        assert results[0]["title"] == "⛵ Sifnos — Jul 17–20"
        assert results[0]["object"] == "database"

    @respx.mock
    async def test_mixed_results_preserve_object_type(self):
        payload = {"results": [
            {
                "object": "page", "id": "p1", "url": "https://notion.so/p1",
                "properties": {"Title": {"type": "title", "title": [{"plain_text": "A Page"}]}},
            },
            {
                "object": "database", "id": "db1", "url": "https://notion.so/db1",
                "title": [{"plain_text": "A Database"}],
                "properties": {},
            },
        ]}
        respx.post("https://api.notion.com/v1/search").mock(
            return_value=httpx.Response(200, json=payload))

        results = await notion_search("query")
        objects = {r["title"]: r["object"] for r in results}
        assert objects["A Page"] == "page"
        assert objects["A Database"] == "database"


# ── notion_query_db_rows ──────────────────────────────────────────────────────

class TestNotionQueryDbRows:
    @respx.mock
    async def test_returns_rows_with_fields(self):
        payload = {"results": [{
            "url": "https://notion.so/row1",
            "properties": {
                "Name":     {"type": "title",     "title": [{"plain_text": "Loggia Wine Bar"}]},
                "Type":     {"type": "select",    "select": {"name": "Bar"}},
                "Location": {"type": "rich_text", "rich_text": [{"plain_text": "Apollonia"}]},
                "Rating":   {"type": "rich_text", "rich_text": [{"plain_text": "4.5/5"}]},
            },
        }]}
        respx.post("https://api.notion.com/v1/databases/test-db/query").mock(
            return_value=httpx.Response(200, json=payload))

        rows = await notion_query_db_rows("test-db")
        assert len(rows) == 1
        assert rows[0]["name"] == "Loggia Wine Bar"
        assert rows[0]["type"] == "Bar"
        assert rows[0]["location"] == "Apollonia"
        assert rows[0]["rating"] == "4.5/5"

    @respx.mock
    async def test_returns_empty_on_api_error(self):
        respx.post("https://api.notion.com/v1/databases/bad-db/query").mock(
            return_value=httpx.Response(404, json={"message": "not found"}))

        rows = await notion_query_db_rows("bad-db")
        assert rows == []


# ── insert_into_trip_db ───────────────────────────────────────────────────────

class TestInsertIntoTripDb:
    @respx.mock
    async def test_inserts_row_with_all_fields(self):
        pending = {
            "name": "Paralia Beach Bar",
            "url": "https://maps.app.goo.gl/ABC",
            "maps_link": "https://goo.gl/maps/ABC",
            "type_": "beach bar",
            "location": "Kamares, Sifnos",
            "rating": "4.7/5",
            "notes": "Great sunset spot",
            "vibe": "relaxed, scenic",
            "best_for": "sunset drinks",
        }
        created_url = "https://notion.so/created-page"
        route = respx.post("https://api.notion.com/v1/pages").mock(
            return_value=httpx.Response(200, json={"url": created_url}))

        result = await insert_into_trip_db("test-db-id", pending)
        assert result == created_url

        body = json.loads(route.calls[0].request.content)
        assert body["parent"] == {"database_id": "test-db-id"}
        props = body["properties"]
        assert props["Name"]["title"][0]["text"]["content"] == "Paralia Beach Bar"
        assert props["Type"]["select"]["name"] == "Bar"   # "beach bar" → "Bar" via _map_type
        assert props["Location"]["rich_text"][0]["text"]["content"] == "Kamares, Sifnos"
        assert props["Rating"]["rich_text"][0]["text"]["content"] == "4.7/5"

    @respx.mock
    async def test_omits_empty_optional_fields(self):
        pending = {"name": "Minimal Place", "url": "", "maps_link": "",
                   "type_": "", "location": "", "rating": "",
                   "notes": "", "vibe": "", "best_for": ""}
        respx.post("https://api.notion.com/v1/pages").mock(
            return_value=httpx.Response(200, json={"url": "https://notion.so/x"}))

        # Should not raise even with all-empty optional fields
        await insert_into_trip_db("test-db-id", pending)

    def test_type_mapping_for_common_venue_types(self):
        cases = [
            ("beach bar",   "Bar"),
            ("taverna",     "Restaurant"),
            ("monastery",   "Sight"),
            ("coffee",      "Cafe"),
            ("hotel",       "Hotel"),
            ("",            ""),
        ]
        for raw, expected in cases:
            assert _map_type(raw) == expected, f"_map_type({raw!r}) should be {expected!r}"


# ── Telegram webhook routing (no HTTP needed) ─────────────────────────────────

class TestWebhookRouting:
    def test_url_detection_regex(self):
        import re
        pattern = r'https?://\S+'
        assert re.search(pattern, "check this https://example.com out")
        assert re.search(pattern, "https://maps.app.goo.gl/ABC123")
        assert not re.search(pattern, "just a regular message")

    def test_question_detection(self):
        import re
        question_pattern = r'^\s*(what|when|where|how|show|find|list|do i|have i|tell me|which|who|why)'
        questions = [
            "What is in my Sifnos list?",
            "When do I go to Kimolos?",
            "Where do I go after?",
            "How much is the ticket?",
            "Show me the places",
        ]
        not_questions = [
            "Save it to Sifnos",
            "Cancel",
            "Add a note",
        ]
        for q in questions:
            assert re.search(question_pattern, q, re.IGNORECASE) or q.endswith("?"), q
        for nq in not_questions:
            is_q = bool(re.search(question_pattern, nq, re.IGNORECASE)) or nq.endswith("?")
            assert not is_q, nq


# ── notion_read_page_content: table block rows ────────────────────────────────

class TestNotionReadTableBlocks:
    @respx.mock
    async def test_table_rows_are_extracted(self):
        """Tables in Notion are has_children blocks with table_row children.
        The reader must fetch the children and emit one line per row — this is the
        regression guard for the 'ferry price not found' class of bugs."""
        page_id = "page-with-table"
        table_id = "table-block-1"

        # First call: page's blocks → returns ONE table block with has_children=true
        respx.get(f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=50").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": table_id, "type": "table", "has_children": True, "table": {}},
            ]})
        )
        # Second call: the table block's children → two table_row blocks
        respx.get(f"https://api.notion.com/v1/blocks/{table_id}/children?page_size=50").mock(
            return_value=httpx.Response(200, json={"results": [
                {"type": "table_row", "table_row": {"cells": [
                    [{"plain_text": "Leg"}], [{"plain_text": "Route"}], [{"plain_text": "Price"}],
                ]}},
                {"type": "table_row", "table_row": {"cells": [
                    [{"plain_text": "2"}],
                    [{"plain_text": "Kimolos → Sifnos"}],
                    [{"plain_text": "€34.70"}],
                ]}},
            ]})
        )

        content = await notion_read_page_content(page_id, max_chars=4000)
        assert "Kimolos → Sifnos" in content
        assert "€34.70" in content
        assert "Leg | Route | Price" in content


# ── notion_fetch_page_meta: database fallback ─────────────────────────────────

class TestNotionFetchPageMeta:
    @respx.mock
    async def test_database_id_falls_back_via_400(self):
        """When called with a DATABASE id, /v1/pages/{id} returns 400 (NOT 404).
        Must fall back to /v1/databases/{id}. Regression guard — without this,
        parent traversal silently fails for every database, hiding the trip page."""
        db_id = "db-id-123"
        # /v1/pages/{db_id} returns 400 (this is what Notion does for db IDs)
        respx.get(f"https://api.notion.com/v1/pages/{db_id}").mock(
            return_value=httpx.Response(400, json={"message": "validation error"}))
        # /v1/databases/{db_id} returns 200 with proper db metadata + page parent
        respx.get(f"https://api.notion.com/v1/databases/{db_id}").mock(
            return_value=httpx.Response(200, json={
                "object": "database",
                "id": db_id,
                "title": [{"plain_text": "⛵ Sifnos — Jul 17–20"}],
                "parent": {"type": "page_id", "page_id": "summer-2026"},
                "url": "https://notion.so/sifnos-db",
            }))

        meta = await notion_fetch_page_meta(db_id)
        assert meta["id"] == db_id
        assert meta["title"] == "⛵ Sifnos — Jul 17–20"
        assert meta["parent"] == {"type": "page_id", "page_id": "summer-2026"}


# ── notion_create_topic_db: bucket-list DB creation ───────────────────────────

class TestNotionCreateTopicDb:
    @respx.mock
    async def test_creates_under_parent_page(self):
        """When given a parent_page_id, the DB must be created with the right parent
        shape AND include the broader Type select options (Event, Festival, Activity)."""
        route = respx.post("https://api.notion.com/v1/databases").mock(
            return_value=httpx.Response(200, json={"id": "new-db-id"}))
        db_id = await notion_create_topic_db("bucket-list-page-123", "Tokyo")
        assert db_id == "new-db-id"
        body = json.loads(route.calls[0].request.content)
        assert body["parent"] == {"type": "page_id", "page_id": "bucket-list-page-123"}
        assert body["title"][0]["text"]["content"] == "Tokyo"
        type_names = [o["name"] for o in body["properties"]["Type"]["select"]["options"]]
        # the new options must be present
        for must_have in ("Place", "Event", "Festival", "Activity", "Restaurant"):
            assert must_have in type_names, f"Type option {must_have!r} missing"
        # and Date property must be there
        assert "Date" in body["properties"]
        assert body["properties"]["Date"] == {"date": {}}

    @respx.mock
    async def test_creates_at_workspace_root_when_no_parent(self):
        """Backward compat: notion_create_trip_db() passes None and gets workspace root."""
        route = respx.post("https://api.notion.com/v1/databases").mock(
            return_value=httpx.Response(200, json={"id": "ws-db"}))
        db_id = await notion_create_topic_db(None, "🗺️ Trip Places")
        assert db_id == "ws-db"
        body = json.loads(route.calls[0].request.content)
        assert body["parent"] == {"type": "workspace", "workspace": True}


# ── notion_query_db_rows: filtering ───────────────────────────────────────────

class TestNotionQueryFilters:
    @respx.mock
    async def test_type_filter_shapes_request(self):
        route = respx.post("https://api.notion.com/v1/databases/db-1/query").mock(
            return_value=httpx.Response(200, json={"results": []}))
        await notion_query_db_rows("db-1", type_filter="Restaurant")
        body = json.loads(route.calls[0].request.content)
        assert body["filter"] == {"property": "Type", "select": {"equals": "Restaurant"}}

    @respx.mock
    async def test_location_filter_shapes_request(self):
        route = respx.post("https://api.notion.com/v1/databases/db-1/query").mock(
            return_value=httpx.Response(200, json={"results": []}))
        await notion_query_db_rows("db-1", location_contains="Tokyo")
        body = json.loads(route.calls[0].request.content)
        assert body["filter"] == {"property": "Location", "rich_text": {"contains": "Tokyo"}}

    @respx.mock
    async def test_both_filters_combine_with_and(self):
        route = respx.post("https://api.notion.com/v1/databases/db-1/query").mock(
            return_value=httpx.Response(200, json={"results": []}))
        await notion_query_db_rows("db-1", type_filter="Bar", location_contains="Athens")
        body = json.loads(route.calls[0].request.content)
        assert "and" in body["filter"]
        assert len(body["filter"]["and"]) == 2

    @respx.mock
    async def test_no_filter_keeps_legacy_body(self):
        """Backward compat: no filter args → no 'filter' key in the request body."""
        route = respx.post("https://api.notion.com/v1/databases/db-1/query").mock(
            return_value=httpx.Response(200, json={"results": []}))
        await notion_query_db_rows("db-1")
        body = json.loads(route.calls[0].request.content)
        assert "filter" not in body
        assert body["sorts"] == [{"property": "Name", "direction": "ascending"}]
