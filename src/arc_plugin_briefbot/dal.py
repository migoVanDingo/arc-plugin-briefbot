"""Read-only DAL over a Briefbot SQLite database.

Three small classes, sync `sqlite3`. arc v2 has no ORM by design; the DAL
returns plain dicts (`sqlite3.Row` rows converted with `dict()`), and tools
read fields by name.

Queries are lifted from v1's SQLModel DAL (`src/db/dal/briefbot/*`) and
flattened into raw SQL. The shapes (parameters, order-by behavior, return
columns) match v1 so tool output stays consistent with what the user is
used to from v1.

Connection management is OUT of scope here — the plugin owns the
connection lifecycle (open in on_session_start, close in on_session_end).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """sqlite3.Row → plain dict. Tools see plain dicts, not Row objects."""
    return {k: row[k] for k in row.keys()}


def _cutoff(days: int) -> str:
    """ISO date N days ago, used as `fetched_at >= cutoff` in queries."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


class ItemsDAL:
    """Queries over the `items` table.

    Schema (relevant columns):
      item_id TEXT PRIMARY KEY
      title TEXT, url TEXT, canonical_url TEXT
      summary TEXT, author TEXT
      source_id TEXT, source_name TEXT, source_category TEXT
      published_at TEXT, fetched_at TEXT
      score REAL, score_opportunity REAL
      opportunity_reason TEXT
      tags_json TEXT (JSON list)
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def count(self) -> int:
        cur = self._c.execute("SELECT COUNT(*) FROM items")
        return int(cur.fetchone()[0])

    def search(
        self,
        *,
        query: str,
        days: int = 30,
        category: str | None = None,
        limit: int = 15,
        order_by: str = "score",
    ) -> list[dict[str, Any]]:
        """Title+summary LIKE search with recency + category filters.

        `order_by`: "score" (default — items.score DESC) or "date"
        (fetched_at DESC). Anything else → "score".
        """
        if order_by not in ("score", "date"):
            order_by = "score"

        sql = (
            "SELECT * FROM items "
            "WHERE fetched_at >= :cutoff "
            "  AND (LOWER(title) LIKE :like OR LOWER(IFNULL(summary,'')) LIKE :like)"
        )
        params: dict[str, Any] = {
            "cutoff": _cutoff(days),
            "like": f"%{query.lower()}%",
        }
        if category:
            sql += " AND source_category = :category"
            params["category"] = category
        if order_by == "date":
            sql += " ORDER BY fetched_at DESC"
        else:
            sql += " ORDER BY score DESC"
        sql += " LIMIT :limit"
        params["limit"] = int(limit)

        cur = self._c.execute(sql, params)
        return [_row_to_dict(r) for r in cur.fetchall()]

    def get_by_id(self, item_id: str) -> dict[str, Any] | None:
        cur = self._c.execute(
            "SELECT * FROM items WHERE item_id = ?", (item_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(row) if row is not None else None


class ClustersDAL:
    """Queries over the `clusters` table.

    Schema (relevant columns):
      cluster_id TEXT PRIMARY KEY
      label TEXT
      item_count INTEGER, sources_count INTEGER
      velocity_1d INTEGER, velocity_3d INTEGER, velocity_7d INTEGER
      trend_score REAL
      representative_title TEXT, representative_url TEXT
    """

    _ALLOWED_WINDOWS = ("1d", "3d", "7d")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def get_trending(
        self,
        *,
        window: str = "3d",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Clusters ordered by `velocity_<window> DESC, trend_score DESC`.

        Unknown windows fall back to "3d" rather than raising — the model
        sometimes invents window values and we'd rather degrade gracefully
        than send a ToolError up.
        """
        if window not in self._ALLOWED_WINDOWS:
            window = "3d"
        # `window` is validated against the closed allowlist above, so the
        # f-string interpolation is safe (we can't bind a column name to
        # a placeholder in standard sqlite3).
        sql = (
            f"SELECT * FROM clusters "
            f"ORDER BY velocity_{window} DESC, trend_score DESC "
            f"LIMIT :limit"
        )
        cur = self._c.execute(sql, {"limit": int(limit)})
        return [_row_to_dict(r) for r in cur.fetchall()]


class TopicsDAL:
    """Queries over the `topic_profiles` table.

    Schema (relevant columns):
      topic_id TEXT PRIMARY KEY, name TEXT, kind TEXT  ('entity' | 'token')
      count_1d, count_3d, count_7d, count_30d INTEGER
      momentum REAL
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def get_top_topics(
        self,
        *,
        limit: int = 10,
        min_momentum: float = 0.0,
    ) -> list[dict[str, Any]]:
        cur = self._c.execute(
            "SELECT * FROM topic_profiles "
            "WHERE momentum >= :min_m "
            "ORDER BY momentum DESC, last_seen_at DESC "
            "LIMIT :limit",
            {"min_m": float(min_momentum), "limit": int(limit)},
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


# ── Schema check ──────────────────────────────────────────────────────────


# Columns the plugin reads. If any are missing, the live Briefbot DB is
# older than we know how to query against — surface as briefbot.schema_mismatch
# rather than crashing mid-turn with a cryptic OperationalError.
REQUIRED_ITEM_COLUMNS = frozenset({
    "item_id", "title", "url", "canonical_url",
    "summary", "author",
    "source_id", "source_name", "source_category",
    "published_at", "fetched_at",
    "score", "score_opportunity", "opportunity_reason",
    "tags_json",
})

REQUIRED_CLUSTER_COLUMNS = frozenset({
    "cluster_id", "label", "item_count",
    "velocity_1d", "velocity_3d", "velocity_7d",
    "trend_score", "representative_title", "representative_url",
})

REQUIRED_TOPIC_COLUMNS = frozenset({
    "topic_id", "name", "kind", "momentum",
    "count_1d", "count_3d", "count_7d",
})


def check_schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Verify the DB has the columns the DAL reads.

    Returns a dict mapping table → list of missing column names. An empty
    dict means the schema is fully compatible.
    """
    missing: dict[str, list[str]] = {}
    for table, required in (
        ("items", REQUIRED_ITEM_COLUMNS),
        ("clusters", REQUIRED_CLUSTER_COLUMNS),
        ("topic_profiles", REQUIRED_TOPIC_COLUMNS),
    ):
        try:
            cur = conn.execute(f"PRAGMA table_info({table})")
            present = {row[1] for row in cur.fetchall()}  # row[1] is column name
        except sqlite3.OperationalError:
            present = set()
        absent = sorted(required - present)
        if absent:
            missing[table] = absent
    return missing
