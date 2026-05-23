"""BriefbotPlugin — lifecycle owner for the Briefbot SQLite handle.

Opens the DB read-only at session start, runs a schema-compatibility check,
hands the connection to three tool instances, and closes everything at
session end. Quarantines cleanly when the DB is missing or schema-incompatible
so the rest of the session continues uninterrupted.

Entry point (declared in pyproject.toml):
    [project.entry-points."arc.plugins"]
    briefbot = "arc_plugin_briefbot.plugin:build"
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from arc.plugin_api import (
    PluginBuildContext,
    RuntimeEvent,
    SessionContext,
    Tool,
    TurnOutcome,
)

from arc_plugin_briefbot.dal import (
    ClustersDAL,
    ItemsDAL,
    TopicsDAL,
    check_schema,
)
from arc_plugin_briefbot.tools.briefbot_item import BriefbotItemTool
from arc_plugin_briefbot.tools.briefbot_search import BriefbotSearchTool
from arc_plugin_briefbot.tools.briefbot_trending import BriefbotTrendingTool


# Default location to probe when neither config nor env var supplies a path.
# Matches upstream Briefbot's typical local install. Most users will have
# the env var set instead; this default mostly exists so the plugin "just
# works" for the canonical layout.
_DEFAULT_DB_PATH = Path.home() / ".briefbot" / "briefbot.db"

# The env var that v1 has used historically; we honor it for compatibility
# with existing user .env files.
_ENV_DB_PATH = "BRIEFBOT_DB_PATH"


class BriefbotPlugin:
    """Session-scoped owner of the Briefbot DB handle + three tools."""

    name = "briefbot"

    def __init__(
        self,
        *,
        db_path: Path | None,
        search_default_days: int = 30,
        search_default_limit: int = 15,
        trending_default_window: str = "3d",
        max_summary_chars: int = 1200,
        disabled_tools: list[str] | None = None,
    ) -> None:
        self._db_path = db_path
        self._search_default_days = search_default_days
        self._search_default_limit = search_default_limit
        self._trending_default_window = trending_default_window
        self._max_summary_chars = max_summary_chars
        self._disabled_tools = set(disabled_tools or [])

        self._conn: sqlite3.Connection | None = None
        self._tools: list[Tool] = []
        self._bus: Any = None

    # ── Bus ────────────────────────────────────────────────────────────────

    def bind_bus(self, bus: Any) -> None:
        self._bus = bus

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_session_start(self, ctx: SessionContext) -> None:
        """Open the DB and build the tools. Graceful no-op on failure."""
        if self._db_path is None:
            self._emit("briefbot.disabled", {
                "reason": f"no DB path configured (set {_ENV_DB_PATH} or plugins.briefbot.config.db_path)",
                "path": None,
            })
            return
        if not self._db_path.exists():
            self._emit("briefbot.disabled", {
                "reason": "DB path does not exist",
                "path": str(self._db_path),
            })
            return

        try:
            conn = sqlite3.connect(
                f"file:{self._db_path}?mode=ro&immutable=1",
                uri=True,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
        except sqlite3.OperationalError as exc:
            self._emit("briefbot.disabled", {
                "reason": f"sqlite open failed: {exc}",
                "path": str(self._db_path),
            })
            return

        # Column probe: the live schema must have everything the DAL reads.
        # Briefbot's PRAGMA user_version is currently 0 (unused), so we check
        # actual columns rather than a version number.
        missing = check_schema(conn)
        if missing:
            self._emit("briefbot.schema_mismatch", {
                "path": str(self._db_path),
                "missing_columns": missing,
            })
            conn.close()
            return

        self._conn = conn
        items_dal = ItemsDAL(conn)
        clusters_dal = ClustersDAL(conn)
        topics_dal = TopicsDAL(conn)

        candidate_tools: list[Tool] = [
            BriefbotSearchTool(
                items_dal=items_dal,
                default_days=self._search_default_days,
                default_limit=self._search_default_limit,
                max_summary_chars=self._max_summary_chars,
            ),
            BriefbotItemTool(
                items_dal=items_dal,
                max_summary_chars=self._max_summary_chars,
            ),
            BriefbotTrendingTool(
                clusters_dal=clusters_dal,
                topics_dal=topics_dal,
                default_window=self._trending_default_window,
            ),
        ]
        self._tools = [t for t in candidate_tools if t.name not in self._disabled_tools]

        # Hand the bus to any tool that wants it — the search/item/trending
        # tools emit briefbot.query for per-call observability.
        for t in self._tools:
            binder = getattr(t, "bind_bus", None)
            if callable(binder) and self._bus is not None:
                binder(self._bus)

        self._emit("briefbot.ready", {
            "path": str(self._db_path),
            "item_count": items_dal.count(),
            "tools": [t.name for t in self._tools],
        })

    def on_session_end(self, ctx: SessionContext, outcome: TurnOutcome | None) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 — close is best-effort
                pass
            self._conn = None
        self._tools = []

    # ── Tool contribution ──────────────────────────────────────────────────

    def provides_tools(self) -> list[Tool]:
        return list(self._tools)

    # ── Internal ───────────────────────────────────────────────────────────

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._bus is None:
            return
        self._bus.emit(RuntimeEvent(
            type=event_type,
            payload=payload,
            stage="plugin",
        ))


# ── Entry point ────────────────────────────────────────────────────────────


def build(config: dict, build_ctx: PluginBuildContext) -> BriefbotPlugin:
    """Construct the plugin from arc's plugin-config dict.

    Config keys (all optional):
      db_path:                  str | None — explicit path
      search_default_days:      int (30)
      search_default_limit:     int (15)
      trending_default_window:  '1d' | '3d' | '7d' ('3d')
      max_summary_chars:        int (1200)
      disabled_tools:           list[str] — names to drop ([])

    Path resolution:
      1. config['db_path'] if non-empty
      2. $BRIEFBOT_DB_PATH env var if set
      3. ~/.briefbot/briefbot.db if it exists
      4. None → plugin runs in disabled mode (no tools contributed)
    """
    plugin = BriefbotPlugin(
        db_path=_resolve_db_path(config.get("db_path")),
        search_default_days=int(config.get("search_default_days", 30)),
        search_default_limit=int(config.get("search_default_limit", 15)),
        trending_default_window=str(config.get("trending_default_window", "3d")),
        max_summary_chars=int(config.get("max_summary_chars", 1200)),
        disabled_tools=list(config.get("disabled_tools", []) or []),
    )
    if build_ctx.bus is not None:
        plugin.bind_bus(build_ctx.bus)
    return plugin


def _resolve_db_path(config_path: str | None) -> Path | None:
    if config_path:
        return Path(config_path).expanduser()
    env_path = os.environ.get(_ENV_DB_PATH)
    if env_path:
        return Path(env_path).expanduser()
    if _DEFAULT_DB_PATH.exists():
        return _DEFAULT_DB_PATH
    return None
