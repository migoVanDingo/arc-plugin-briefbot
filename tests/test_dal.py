"""DAL tests — exercise the SQL against a freshly-built mini Briefbot DB.

The fixture in conftest seeds known rows; assertions reference them by id
(itm_001, clu_002, top_001, etc.) so the test failures are diagnostic when
the SQL or fixture changes.
"""
from __future__ import annotations

import pytest

from arc_plugin_briefbot.dal import (
    ClustersDAL,
    ItemsDAL,
    TopicsDAL,
    check_schema,
)


# ── ItemsDAL ─────────────────────────────────────────────────────────────


def test_items_count(mini_db_conn):
    dal = ItemsDAL(mini_db_conn)
    assert dal.count() == 6


def test_items_search_matches_title(mini_db_conn):
    dal = ItemsDAL(mini_db_conn)
    results = dal.search(query="ghidra", days=30, limit=10, order_by="score")
    ids = [r["item_id"] for r in results]
    # itm_001 + itm_005 both mention Ghidra in title/summary
    assert "itm_001" in ids
    assert "itm_005" in ids


def test_items_search_matches_summary(mini_db_conn):
    """Summary-only match: 'frame parser' is in itm_003's summary, nowhere else."""
    dal = ItemsDAL(mini_db_conn)
    results = dal.search(query="frame parser", days=30, limit=10)
    ids = [r["item_id"] for r in results]
    assert ids == ["itm_003"]


def test_items_search_respects_days_window(mini_db_conn):
    """itm_006 is 90 days old and should never show up in a 30-day search."""
    dal = ItemsDAL(mini_db_conn)
    recent = dal.search(query="news", days=30, limit=10)
    assert "itm_006" not in {r["item_id"] for r in recent}
    # But with a 120-day window it should
    older = dal.search(query="news", days=120, limit=10)
    assert "itm_006" in {r["item_id"] for r in older}


def test_items_search_filters_by_category(mini_db_conn):
    dal = ItemsDAL(mini_db_conn)
    # itm_001 (devtools) and itm_005 (devtools) both match "ghidra"
    devtools_only = dal.search(query="ghidra", category="devtools", days=30, limit=10)
    cats = {r["source_category"] for r in devtools_only}
    assert cats == {"devtools"}
    # Bogus category → empty
    empty = dal.search(query="ghidra", category="ai_research", days=30, limit=10)
    assert empty == []


def test_items_search_order_by_score_descending(mini_db_conn):
    """Default ordering: highest score first."""
    dal = ItemsDAL(mini_db_conn)
    results = dal.search(query="ghidra", days=30, limit=10, order_by="score")
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_items_search_order_by_date(mini_db_conn):
    """`date` order picks most recent first."""
    dal = ItemsDAL(mini_db_conn)
    results = dal.search(query="ghidra", days=30, limit=10, order_by="date")
    dates = [r["fetched_at"] for r in results]
    assert dates == sorted(dates, reverse=True)


def test_items_search_invalid_order_by_falls_back_to_score(mini_db_conn):
    dal = ItemsDAL(mini_db_conn)
    results = dal.search(query="ghidra", days=30, limit=10, order_by="garbage")
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_items_search_respects_limit(mini_db_conn):
    dal = ItemsDAL(mini_db_conn)
    results = dal.search(query="", days=30, limit=2)  # empty query matches all
    assert len(results) <= 2


def test_items_get_by_id_hit_and_miss(mini_db_conn):
    dal = ItemsDAL(mini_db_conn)
    hit = dal.get_by_id("itm_002")
    assert hit is not None
    assert hit["title"] == "Transformer attention revisited"
    assert dal.get_by_id("nope") is None


# ── ClustersDAL ──────────────────────────────────────────────────────────


def test_clusters_get_trending_3d_default(mini_db_conn):
    """Default window=3d → ordered by velocity_3d DESC, then trend_score DESC."""
    dal = ClustersDAL(mini_db_conn)
    results = dal.get_trending(limit=10)
    ids = [r["cluster_id"] for r in results]
    # velocity_3d order: clu_002 (7) > clu_001 (4) > clu_003 (2)
    assert ids == ["clu_002", "clu_001", "clu_003"]


def test_clusters_get_trending_1d(mini_db_conn):
    """velocity_1d: clu_002 (3) > clu_001 (1) > clu_003 (0)."""
    dal = ClustersDAL(mini_db_conn)
    results = dal.get_trending(window="1d", limit=10)
    ids = [r["cluster_id"] for r in results]
    assert ids == ["clu_002", "clu_001", "clu_003"]


def test_clusters_get_trending_7d(mini_db_conn):
    """velocity_7d: clu_002 (18) > clu_001 (8) > clu_003 (3)."""
    dal = ClustersDAL(mini_db_conn)
    results = dal.get_trending(window="7d", limit=10)
    ids = [r["cluster_id"] for r in results]
    assert ids == ["clu_002", "clu_001", "clu_003"]


def test_clusters_invalid_window_falls_back(mini_db_conn):
    """Garbage window → defaults to 3d, no exception."""
    dal = ClustersDAL(mini_db_conn)
    results = dal.get_trending(window="garbage", limit=10)
    assert len(results) == 3


def test_clusters_get_trending_respects_limit(mini_db_conn):
    dal = ClustersDAL(mini_db_conn)
    results = dal.get_trending(limit=1)
    assert len(results) == 1


# ── TopicsDAL ────────────────────────────────────────────────────────────


def test_topics_get_top_descending_momentum(mini_db_conn):
    dal = TopicsDAL(mini_db_conn)
    results = dal.get_top_topics(limit=10)
    momentums = [r["momentum"] for r in results]
    assert momentums == sorted(momentums, reverse=True)


def test_topics_min_momentum_filter(mini_db_conn):
    """min_momentum filters out the bottom topic (scaling laws @ 0.10)."""
    dal = TopicsDAL(mini_db_conn)
    results = dal.get_top_topics(limit=10, min_momentum=0.5)
    names = [r["name"] for r in results]
    assert "scaling laws" not in names


def test_topics_limit(mini_db_conn):
    dal = TopicsDAL(mini_db_conn)
    results = dal.get_top_topics(limit=2)
    assert len(results) == 2


# ── Schema check ─────────────────────────────────────────────────────────


def test_check_schema_ok_on_full_db(mini_db_conn):
    """Mini DB has every column we read; should report no missing."""
    assert check_schema(mini_db_conn) == {}


def test_check_schema_reports_missing_columns(tmp_path):
    """A DB with a stripped `items` table should be flagged."""
    import sqlite3
    p = tmp_path / "broken.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE items (item_id TEXT, title TEXT)")  # missing tons
    conn.execute("CREATE TABLE clusters (cluster_id TEXT)")
    conn.execute("CREATE TABLE topic_profiles (topic_id TEXT)")
    conn.commit()
    missing = check_schema(conn)
    conn.close()
    assert "items" in missing
    assert "score" in missing["items"]
    assert "source_category" in missing["items"]


def test_check_schema_reports_missing_tables(tmp_path):
    """A DB missing whole tables: every required column shows up as missing."""
    import sqlite3
    p = tmp_path / "empty.db"
    conn = sqlite3.connect(str(p))
    missing = check_schema(conn)
    conn.close()
    assert set(missing.keys()) == {"items", "clusters", "topic_profiles"}
