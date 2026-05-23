"""Plugin lifecycle tests — open/close behavior, graceful degradation,
config resolution, tool wiring.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arc_plugin_briefbot.plugin import (
    BriefbotPlugin,
    _resolve_db_path,
    build,
)


# ── build() entry point ──────────────────────────────────────────────────


def test_build_applies_config(build_ctx, mini_db_path):
    plugin = build(
        {
            "db_path": str(mini_db_path),
            "search_default_days": 60,
            "search_default_limit": 25,
            "trending_default_window": "7d",
            "max_summary_chars": 500,
            "disabled_tools": ["briefbot_trending"],
        },
        build_ctx,
    )
    assert isinstance(plugin, BriefbotPlugin)
    assert plugin.name == "briefbot"
    assert plugin._db_path == mini_db_path
    assert plugin._search_default_days == 60
    assert plugin._search_default_limit == 25
    assert plugin._trending_default_window == "7d"
    assert plugin._max_summary_chars == 500
    assert "briefbot_trending" in plugin._disabled_tools


def test_build_applies_defaults(build_ctx, mini_db_path):
    plugin = build({"db_path": str(mini_db_path)}, build_ctx)
    assert plugin._search_default_days == 30
    assert plugin._search_default_limit == 15
    assert plugin._trending_default_window == "3d"


def test_build_binds_bus_from_context(build_ctx, mini_db_path):
    plugin = build({"db_path": str(mini_db_path)}, build_ctx)
    assert plugin._bus is build_ctx.bus


# ── Path resolution ──────────────────────────────────────────────────────


def test_resolve_db_path_prefers_config(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit.db"
    explicit.touch()
    monkeypatch.setenv("BRIEFBOT_DB_PATH", "/should/be/ignored")
    assert _resolve_db_path(str(explicit)) == explicit


def test_resolve_db_path_falls_back_to_env(monkeypatch, tmp_path):
    env_path = tmp_path / "env.db"
    env_path.touch()
    monkeypatch.setenv("BRIEFBOT_DB_PATH", str(env_path))
    assert _resolve_db_path(None) == env_path


def test_resolve_db_path_returns_none_when_unresolved(monkeypatch):
    monkeypatch.delenv("BRIEFBOT_DB_PATH", raising=False)
    # Default location ~/.briefbot/briefbot.db doesn't exist on this box
    # in CI; if it does for the developer running locally, this test is
    # a no-op and that's fine.
    from arc_plugin_briefbot.plugin import _DEFAULT_DB_PATH
    if not _DEFAULT_DB_PATH.exists():
        assert _resolve_db_path(None) is None


# ── on_session_start: happy path ─────────────────────────────────────────


def test_session_start_opens_db_and_provides_tools(build_ctx, session_ctx, mini_db_path):
    plugin = build({"db_path": str(mini_db_path)}, build_ctx)
    plugin.on_session_start(session_ctx)

    assert "briefbot.ready" in build_ctx.bus.types()
    payload = build_ctx.bus.payloads("briefbot.ready")[0]
    assert payload["item_count"] == 6
    assert set(payload["tools"]) == {
        "briefbot_search", "briefbot_item", "briefbot_trending",
    }

    tool_names = {t.name for t in plugin.provides_tools()}
    assert tool_names == {"briefbot_search", "briefbot_item", "briefbot_trending"}


def test_session_start_propagates_bus_to_tools(build_ctx, session_ctx, mini_db_path):
    plugin = build({"db_path": str(mini_db_path)}, build_ctx)
    plugin.on_session_start(session_ctx)
    for t in plugin.provides_tools():
        assert getattr(t, "_bus", None) is build_ctx.bus


def test_disabled_tools_drops_named_tool(build_ctx, session_ctx, mini_db_path):
    plugin = build(
        {"db_path": str(mini_db_path), "disabled_tools": ["briefbot_trending"]},
        build_ctx,
    )
    plugin.on_session_start(session_ctx)
    tool_names = {t.name for t in plugin.provides_tools()}
    assert "briefbot_trending" not in tool_names
    assert tool_names == {"briefbot_search", "briefbot_item"}


# ── on_session_start: graceful degradation ───────────────────────────────


def test_disabled_when_db_path_is_none(build_ctx, session_ctx, monkeypatch):
    """No path → emit briefbot.disabled, register no tools."""
    monkeypatch.delenv("BRIEFBOT_DB_PATH", raising=False)
    plugin = BriefbotPlugin(db_path=None)
    plugin.bind_bus(build_ctx.bus)
    plugin.on_session_start(session_ctx)

    assert "briefbot.disabled" in build_ctx.bus.types()
    assert plugin.provides_tools() == []


def test_disabled_when_db_path_missing(build_ctx, session_ctx, tmp_path):
    plugin = BriefbotPlugin(db_path=tmp_path / "nope.db")
    plugin.bind_bus(build_ctx.bus)
    plugin.on_session_start(session_ctx)

    assert "briefbot.disabled" in build_ctx.bus.types()
    payload = build_ctx.bus.payloads("briefbot.disabled")[0]
    assert "does not exist" in payload["reason"]
    assert plugin.provides_tools() == []


def test_schema_mismatch_disables_plugin(build_ctx, session_ctx, tmp_path):
    """A DB with missing columns → briefbot.schema_mismatch + no tools."""
    p = tmp_path / "broken.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE items (item_id TEXT, title TEXT)")
    conn.execute("CREATE TABLE clusters (cluster_id TEXT)")
    conn.execute("CREATE TABLE topic_profiles (topic_id TEXT)")
    conn.commit()
    conn.close()

    plugin = BriefbotPlugin(db_path=p)
    plugin.bind_bus(build_ctx.bus)
    plugin.on_session_start(session_ctx)

    assert "briefbot.schema_mismatch" in build_ctx.bus.types()
    payload = build_ctx.bus.payloads("briefbot.schema_mismatch")[0]
    assert "items" in payload["missing_columns"]
    assert plugin.provides_tools() == []


# ── on_session_end ───────────────────────────────────────────────────────


def test_session_end_closes_connection(build_ctx, session_ctx, mini_db_path):
    plugin = build({"db_path": str(mini_db_path)}, build_ctx)
    plugin.on_session_start(session_ctx)
    conn = plugin._conn
    assert conn is not None
    plugin.on_session_end(session_ctx, outcome=None)
    assert plugin._conn is None
    # Operating on the closed connection should raise — proves we actually closed
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_session_end_is_idempotent(build_ctx, session_ctx, mini_db_path):
    plugin = build({"db_path": str(mini_db_path)}, build_ctx)
    plugin.on_session_start(session_ctx)
    plugin.on_session_end(session_ctx, outcome=None)
    plugin.on_session_end(session_ctx, outcome=None)  # second call shouldn't raise


def test_session_end_safe_without_start(build_ctx, session_ctx):
    """Calling end before start (or after a disabled start) must not raise."""
    plugin = BriefbotPlugin(db_path=None)
    plugin.bind_bus(build_ctx.bus)
    plugin.on_session_end(session_ctx, outcome=None)
