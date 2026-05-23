"""Tool tests — input validation, output formatting, event emission.

Each tool gets exercised against the mini DB via its DAL. Stub bus captures
the briefbot.query event so we can assert on the structured payload.
"""
from __future__ import annotations

import pytest

from arc.plugin_api import ToolError

from arc_plugin_briefbot.dal import ClustersDAL, ItemsDAL, TopicsDAL
from arc_plugin_briefbot.tools.briefbot_item import BriefbotItemTool
from arc_plugin_briefbot.tools.briefbot_search import BriefbotSearchTool
from arc_plugin_briefbot.tools.briefbot_trending import BriefbotTrendingTool


# ── briefbot_search ──────────────────────────────────────────────────────


@pytest.fixture
def search_tool(mini_db_conn, bus):
    tool = BriefbotSearchTool(
        items_dal=ItemsDAL(mini_db_conn),
        default_days=30,
        default_limit=15,
        max_summary_chars=200,
    )
    tool.bind_bus(bus)
    return tool


def test_search_returns_results(search_tool):
    out = search_tool.execute({"query": "ghidra"})
    # Both Ghidra items
    assert "Ghidra script API tour" in out
    assert "Sourcegraph adds Ghidra plugin" in out
    # Header includes the query in repr() form
    assert "'ghidra'" in out


def test_search_renders_score_category_source_url(search_tool):
    out = search_tool.execute({"query": "ghidra"})
    assert "score=" in out
    assert "devtools" in out
    assert "https://example.org/" in out
    assert "itm_001" in out  # item id present so model can drill down


def test_search_truncates_long_summary():
    """If a summary is very long, it's clipped to max_summary_chars + '…'."""
    class _FakeDAL:
        def search(self, *, query, days, category, limit, order_by):
            return [{
                "item_id": "x", "title": "T", "url": "u", "canonical_url": "u",
                "score": 0.5, "source_name": "S", "source_category": "devtools",
                "summary": "a" * 1000, "opportunity_reason": None,
            }]
        def count(self): return 1
        def get_by_id(self, x): return None

    tool = BriefbotSearchTool(
        items_dal=_FakeDAL(), default_days=30, default_limit=15, max_summary_chars=100,
    )
    out = tool.execute({"query": "anything"})
    # 100 a's + ellipsis somewhere; not the full 1000
    assert "a" * 100 in out
    assert "a" * 200 not in out


def test_search_empty_query_raises(search_tool):
    with pytest.raises(ToolError, match="required"):
        search_tool.execute({"query": ""})


def test_search_invalid_category_raises(search_tool):
    with pytest.raises(ToolError, match="category"):
        search_tool.execute({"query": "ghidra", "category": "made_up"})


def test_search_no_results_returns_success_string(search_tool):
    """Empty result is a *success* — the model can adapt."""
    out = search_tool.execute({"query": "this_phrase_does_not_exist_anywhere"})
    assert "No results" in out


def test_search_emits_query_event(search_tool, bus):
    search_tool.execute({"query": "ghidra"})
    types = bus.types()
    assert "briefbot.query" in types
    payload = bus.payloads("briefbot.query")[0]
    assert payload["tool"] == "briefbot_search"
    assert payload["query"] == "ghidra"
    assert payload["result_count"] >= 1
    assert "took_ms" in payload


def test_search_caps_limit_at_50(search_tool):
    """The hard cap on limit is 50 even when the model requests more.
    Passing a giant limit should clamp safely, not crash or error."""
    out = search_tool.execute({"query": "ghidra", "limit": 9999})
    # 2 Ghidra items in the fixture; output renders normally
    assert "Briefbot Search" in out
    assert "Ghidra" in out


def test_search_schema_advertises_category_enum(search_tool):
    schema = search_tool.input_schema.to_json_schema()
    cats = schema["properties"]["category"]["enum"]
    assert "ai_research" in cats
    assert "devtools" in cats


# ── briefbot_item ────────────────────────────────────────────────────────


@pytest.fixture
def item_tool(mini_db_conn, bus):
    tool = BriefbotItemTool(
        items_dal=ItemsDAL(mini_db_conn),
        max_summary_chars=2000,
    )
    tool.bind_bus(bus)
    return tool


def test_item_renders_full_record(item_tool):
    out = item_tool.execute({"item_id": "itm_003"})
    assert "CVE-2026-0001 in libfoo" in out
    assert "Project Zero" in out
    assert "security" in out
    assert "Score:" in out
    assert "Opp score:" in out
    assert "Heap overflow" in out  # summary
    assert "Active exploitation" in out  # opportunity_reason


def test_item_renders_tags(item_tool):
    out = item_tool.execute({"item_id": "itm_001"})
    assert "Tags:" in out
    assert "ai" in out


def test_item_missing_id_returns_string(item_tool):
    out = item_tool.execute({"item_id": "nonexistent_id"})
    assert "No item found" in out


def test_item_empty_id_raises(item_tool):
    with pytest.raises(ToolError, match="required"):
        item_tool.execute({"item_id": ""})


def test_item_emits_query_event(item_tool, bus):
    item_tool.execute({"item_id": "itm_001"})
    payload = bus.payloads("briefbot.query")[0]
    assert payload["tool"] == "briefbot_item"
    assert payload["item_id"] == "itm_001"
    assert payload["found"] is True


def test_item_emits_not_found_event(item_tool, bus):
    item_tool.execute({"item_id": "nope"})
    payload = bus.payloads("briefbot.query")[0]
    assert payload["found"] is False


def test_item_handles_corrupt_tags_json(mini_db_conn, bus, tmp_path):
    """Garbage in tags_json shouldn't crash the tool; just no Tags line."""
    class _FakeDAL:
        def get_by_id(self, item_id):
            return {
                "item_id": "x", "title": "T", "url": "u", "canonical_url": "u",
                "score": 0.5, "source_name": "S", "source_category": "devtools",
                "fetched_at": "2026-01-01", "tags_json": "{not valid json",
                "summary": "ok", "opportunity_reason": None,
                "published_at": None, "author": None, "score_opportunity": None,
            }

    tool = BriefbotItemTool(items_dal=_FakeDAL())
    out = tool.execute({"item_id": "x"})
    assert "Tags:" not in out
    assert "ok" in out


# ── briefbot_trending ────────────────────────────────────────────────────


@pytest.fixture
def trending_tool(mini_db_conn, bus):
    tool = BriefbotTrendingTool(
        clusters_dal=ClustersDAL(mini_db_conn),
        topics_dal=TopicsDAL(mini_db_conn),
        default_window="3d",
    )
    tool.bind_bus(bus)
    return tool


def test_trending_default_renders_clusters_and_topics(trending_tool):
    out = trending_tool.execute({})
    assert "Trending Storylines" in out
    assert "Hot Topics" in out
    # Highest velocity_3d cluster appears
    assert "Attention research wave" in out
    # Highest momentum topic appears
    assert "ghidra" in out


def test_trending_window_param_is_honored(trending_tool):
    out = trending_tool.execute({"window": "7d"})
    assert "window: 7d" in out
    assert "velocity_7d=" in out


def test_trending_invalid_window_falls_back(trending_tool):
    """Unknown windows reset to the configured default (3d)."""
    out = trending_tool.execute({"window": "garbage"})
    assert "window: 3d" in out


def test_trending_respects_limits(trending_tool):
    out = trending_tool.execute({"clusters_limit": 1, "topics_limit": 1})
    # Exactly one [1] cluster line, exactly one [1] topic line
    assert out.count("[1]") == 2
    assert "[2]" not in out


def test_trending_emits_query_event(trending_tool, bus):
    trending_tool.execute({"window": "1d"})
    payload = bus.payloads("briefbot.query")[0]
    assert payload["tool"] == "briefbot_trending"
    assert payload["window"] == "1d"
    assert payload["cluster_count"] >= 1
    assert payload["topic_count"] >= 1
