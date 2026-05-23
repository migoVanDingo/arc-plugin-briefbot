"""briefbot_trending — trending clusters + hot topics in the Briefbot corpus."""
from __future__ import annotations

import time
from typing import Any, ClassVar

from arc.plugin_api import RuntimeEvent, ToolInputSchema

from arc_plugin_briefbot.dal import ClustersDAL, TopicsDAL


_VALID_WINDOWS = ("1d", "3d", "7d")


class BriefbotTrendingTool:
    name: ClassVar[str] = "briefbot_trending"
    description: ClassVar[str] = (
        "Return trending storyline clusters and hot topics from the Briefbot "
        "corpus. Clusters are grouped storylines ranked by velocity (items "
        "added per window) and trend_score. Topics are ranked by momentum. "
        "Use this for 'what's trending', 'what's hot in AI', or 'what's new "
        "this week' questions — no query needed."
    )

    def __init__(
        self,
        *,
        clusters_dal: ClustersDAL,
        topics_dal: TopicsDAL,
        default_window: str = "3d",
    ) -> None:
        self._clusters = clusters_dal
        self._topics = topics_dal
        self._default_window = default_window if default_window in _VALID_WINDOWS else "3d"
        self._bus: Any = None

    def bind_bus(self, bus: Any) -> None:
        self._bus = bus

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "window": {
                    "type": "string",
                    "description": f"Velocity window (default {self._default_window}).",
                    "enum": list(_VALID_WINDOWS),
                },
                "clusters_limit": {
                    "type": "integer",
                    "description": "Max trending clusters to return (default 8, hard cap 20).",
                    "minimum": 1,
                    "maximum": 20,
                },
                "topics_limit": {
                    "type": "integer",
                    "description": "Max hot topics to return (default 10, hard cap 30).",
                    "minimum": 1,
                    "maximum": 30,
                },
            },
            required=[],
        )

    def execute(self, input: dict[str, Any]) -> str:
        window = input.get("window", self._default_window)
        if window not in _VALID_WINDOWS:
            window = self._default_window
        clusters_limit = min(int(input.get("clusters_limit", 8)), 20)
        topics_limit = min(int(input.get("topics_limit", 10)), 30)

        t0 = time.perf_counter()
        clusters = self._clusters.get_trending(window=window, limit=clusters_limit)
        topics = self._topics.get_top_topics(limit=topics_limit)
        took_ms = int((time.perf_counter() - t0) * 1000)
        self._emit_query(window, clusters_limit, topics_limit,
                         len(clusters), len(topics), took_ms)

        lines = [f"Briefbot Trending  (window: {window})", ""]

        lines.append(f"=== Trending Storylines ({len(clusters)}) ===")
        if not clusters:
            lines.append("  No clusters found.")
        for i, c in enumerate(clusters, 1):
            label = c.get("label") or "(unlabeled)"
            vel = c.get(f"velocity_{window}", 0)
            trend = c.get("trend_score") or 0.0
            count = c.get("item_count") or 0
            lines.append(f"[{i}] {label}")
            lines.append(f"    trend={float(trend):.2f}  velocity_{window}={vel}  items={count}")
            rep_title = c.get("representative_title")
            if rep_title:
                rep_title = rep_title.replace("\n", " ").strip()
                if len(rep_title) > 80:
                    rep_title = rep_title[:80] + "…"
                lines.append(f"    representative: {rep_title}")
            rep_url = c.get("representative_url")
            if rep_url:
                lines.append(f"    {rep_url}")
            lines.append("")

        lines.append(f"=== Hot Topics ({len(topics)}) ===")
        if not topics:
            lines.append("  No topics found.")
        for i, t in enumerate(topics, 1):
            mom = t.get("momentum") or 0.0
            lines.append(
                f"[{i}] {t.get('name', '?')}  ({t.get('kind', '?')})  "
                f"momentum={float(mom):.2f}  "
                f"7d={t.get('count_7d', 0)}  3d={t.get('count_3d', 0)}  1d={t.get('count_1d', 0)}"
            )

        return "\n".join(lines).rstrip()

    def _emit_query(
        self,
        window: str,
        clusters_limit: int,
        topics_limit: int,
        cluster_count: int,
        topic_count: int,
        took_ms: int,
    ) -> None:
        if self._bus is None:
            return
        self._bus.emit(RuntimeEvent(
            type="briefbot.query",
            stage="tool",
            payload={
                "tool": self.name,
                "window": window,
                "clusters_limit": clusters_limit,
                "topics_limit": topics_limit,
                "cluster_count": cluster_count,
                "topic_count": topic_count,
                "took_ms": took_ms,
            },
        ))
