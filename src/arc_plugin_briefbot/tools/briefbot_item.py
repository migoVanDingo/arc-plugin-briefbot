"""briefbot_item — fetch a single Briefbot item's full details."""
from __future__ import annotations

import json
import time
from typing import Any, ClassVar

from arc.plugin_api import RuntimeEvent, ToolError, ToolInputSchema

from arc_plugin_briefbot.dal import ItemsDAL


class BriefbotItemTool:
    name: ClassVar[str] = "briefbot_item"
    description: ClassVar[str] = (
        "Fetch full details for a specific Briefbot item by item_id. Returns "
        "title, URL, source, scores, tags, opportunity analysis, and summary. "
        "Use this after briefbot_search to get full metadata on a result."
    )

    def __init__(
        self,
        *,
        items_dal: ItemsDAL,
        max_summary_chars: int = 2000,
    ) -> None:
        self._dal = items_dal
        self._max_summary_chars = max_summary_chars
        self._bus: Any = None

    def bind_bus(self, bus: Any) -> None:
        self._bus = bus

    @property
    def input_schema(self) -> ToolInputSchema:
        return ToolInputSchema(
            properties={
                "item_id": {
                    "type": "string",
                    "description": "The Briefbot item_id from a briefbot_search result.",
                },
            },
            required=["item_id"],
        )

    def execute(self, input: dict[str, Any]) -> str:
        item_id = str(input.get("item_id", "")).strip()
        if not item_id:
            raise ToolError("`item_id` is required and must be non-empty")

        t0 = time.perf_counter()
        item = self._dal.get_by_id(item_id)
        took_ms = int((time.perf_counter() - t0) * 1000)
        self._emit_query(item_id, took_ms, found=item is not None)

        if item is None:
            return f"No item found with item_id: {item_id}"

        lines = [f"Briefbot Item: {item['item_id']}", ""]
        lines.append(f"Title:     {item.get('title', '(no title)')}")
        url = item.get("canonical_url") or item.get("url")
        if url:
            lines.append(f"URL:       {url}")
        source = item.get("source_name") or "—"
        cat = item.get("source_category") or "—"
        lines.append(f"Source:    {source}  ({cat})")
        score = item.get("score")
        if score is not None:
            lines.append(f"Score:     {float(score):.3f}")
        opp_score = item.get("score_opportunity")
        if opp_score is not None:
            lines.append(f"Opp score: {float(opp_score):.3f}")
        if item.get("published_at"):
            lines.append(f"Published: {item['published_at']}")
        lines.append(f"Fetched:   {item.get('fetched_at', '—')}")
        if item.get("author"):
            lines.append(f"Author:    {item['author']}")

        tags = _safe_load_json_list(item.get("tags_json"))
        if tags:
            lines.append(f"Tags:      {', '.join(str(t) for t in tags)}")

        summary = (item.get("summary") or "").strip()
        if summary:
            if len(summary) > self._max_summary_chars:
                summary = summary[: self._max_summary_chars].rstrip() + (
                    f"… [+{len(item['summary']) - self._max_summary_chars} chars truncated]"
                )
            lines.append("")
            lines.append("Summary:")
            lines.append(summary)

        opp_reason = (item.get("opportunity_reason") or "").strip()
        if opp_reason:
            lines.append("")
            lines.append("Opportunity:")
            lines.append(opp_reason)

        return "\n".join(lines)

    def _emit_query(self, item_id: str, took_ms: int, *, found: bool) -> None:
        if self._bus is None:
            return
        self._bus.emit(RuntimeEvent(
            type="briefbot.query",
            stage="tool",
            payload={
                "tool": self.name,
                "item_id": item_id,
                "found": found,
                "took_ms": took_ms,
            },
        ))


def _safe_load_json_list(raw: Any) -> list[Any]:
    """Decode `tags_json` defensively. Briefbot stores `'[]'` for empty;
    corrupt rows shouldn't crash the tool."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
