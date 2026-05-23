"""briefbot_search — title+summary search over the local Briefbot corpus."""
from __future__ import annotations

import time
from typing import Any, ClassVar

from arc.plugin_api import RuntimeEvent, ToolError, ToolInputSchema

from arc_plugin_briefbot.dal import ItemsDAL


_VALID_CATEGORIES = (
    "ai_research",
    "ai_industry",
    "mlops_infra",
    "security",
    "devtools",
    "tech_news",
    "aggregator",
    "papers",
)

_VALID_ORDER_BY = ("score", "date")


class BriefbotSearchTool:
    name: ClassVar[str] = "briefbot_search"
    description: ClassVar[str] = (
        "Search the local Briefbot research corpus (nightly-indexed papers, "
        "blog posts, HN/Lobsters, dev-tools and security news from dozens of "
        "curated sources). Returns scored, deduplicated results with title, "
        "URL, source, and summary. Prefer this over web_search for research, "
        "paper, and tech-topic queries."
    )

    def __init__(
        self,
        *,
        items_dal: ItemsDAL,
        default_days: int = 30,
        default_limit: int = 15,
        max_summary_chars: int = 200,
    ) -> None:
        self._dal = items_dal
        self._default_days = default_days
        self._default_limit = default_limit
        self._max_summary_chars = max_summary_chars
        self._bus: Any = None

    def bind_bus(self, bus: Any) -> None:
        self._bus = bus

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "query": {
                    "type": "string",
                    "description": "Search terms matched against title and summary.",
                },
                "days": {
                    "type": "integer",
                    "description": f"Recency window in days (default {self._default_days}).",
                    "minimum": 1,
                    "maximum": 365,
                },
                "category": {
                    "type": "string",
                    "description": "Filter by source category.",
                    "enum": list(_VALID_CATEGORIES),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results (default {self._default_limit}, hard cap 50).",
                    "minimum": 1,
                    "maximum": 50,
                },
                "order_by": {
                    "type": "string",
                    "description": "Sort by 'score' (default) or 'date'.",
                    "enum": list(_VALID_ORDER_BY),
                },
            },
            required=["query"],
        )

    def execute(self, input: dict[str, Any]) -> str:
        query = str(input.get("query", "")).strip()
        if not query:
            raise ToolError("`query` is required and must be non-empty")

        days = int(input.get("days", self._default_days))
        category = input.get("category") or None
        if category and category not in _VALID_CATEGORIES:
            raise ToolError(
                f"`category` must be one of {list(_VALID_CATEGORIES)}, got {category!r}"
            )
        limit = min(int(input.get("limit", self._default_limit)), 50)
        order_by = input.get("order_by", "score")
        if order_by not in _VALID_ORDER_BY:
            order_by = "score"

        t0 = time.perf_counter()
        results = self._dal.search(
            query=query, days=days, category=category,
            limit=limit, order_by=order_by,
        )
        took_ms = int((time.perf_counter() - t0) * 1000)
        self._emit_query(query, days, category, limit, order_by, len(results), took_ms)

        if not results:
            return f"No results in Briefbot corpus for {query!r} (last {days}d)"

        header = f"Briefbot Search: {query!r}  ({len(results)} results, last {days}d)"
        lines = [header, ""]
        for i, item in enumerate(results, 1):
            score = item.get("score") or 0.0
            cat = item.get("source_category") or "—"
            source = item.get("source_name") or "—"
            id_str = item.get("item_id", "")
            lines.append(f"[{i}] {item.get('title', '(no title)')}  [score={score:.2f}, {cat}]")
            lines.append(f"    id: {id_str}  source: {source}")
            url = item.get("canonical_url") or item.get("url")
            if url:
                lines.append(f"    {url}")
            summary = (item.get("summary") or "").strip().replace("\n", " ")
            if summary:
                if len(summary) > self._max_summary_chars:
                    summary = summary[: self._max_summary_chars].rstrip() + "…"
                lines.append(f"    {summary}")
            opp = item.get("opportunity_reason")
            if opp:
                opp = opp.strip().replace("\n", " ")
                if len(opp) > 120:
                    opp = opp[:120].rstrip() + "…"
                lines.append(f"    opportunity: {opp}")
            lines.append("")

        return "\n".join(lines).rstrip()

    def _emit_query(
        self,
        query: str,
        days: int,
        category: str | None,
        limit: int,
        order_by: str,
        result_count: int,
        took_ms: int,
    ) -> None:
        if self._bus is None:
            return
        self._bus.emit(RuntimeEvent(
            type="briefbot.query",
            stage="tool",
            payload={
                "tool": self.name,
                "query": query,
                "days": days,
                "category": category,
                "limit": limit,
                "order_by": order_by,
                "result_count": result_count,
                "took_ms": took_ms,
            },
        ))
