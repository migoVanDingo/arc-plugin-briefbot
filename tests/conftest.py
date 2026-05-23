"""Test fixtures for arc-plugin-briefbot.

Provides:
  - StubBus + StubBuildContext (don't depend on arc internals to test)
  - mini_db_path: a small but realistic SQLite fixture, freshly built per
    test, mirroring the real Briefbot schema. Tests assert against known
    rows in it.

The fixture builder is the source of truth for what shape a Briefbot DB
looks like in tests. Update it (not the assertions) when the upstream
schema changes.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


# ── Stub bus ──────────────────────────────────────────────────────────────


class StubBus:
    def __init__(self) -> None:
        self.emitted: list[Any] = []

    def emit(self, event: Any) -> None:
        self.emitted.append(event)

    def types(self) -> list[str]:
        return [getattr(e, "type", "?") for e in self.emitted]

    def payloads(self, type_: str) -> list[dict]:
        return [e.payload for e in self.emitted if getattr(e, "type", None) == type_]


# ── Stub context objects ──────────────────────────────────────────────────


@dataclass(frozen=True)
class StubSessionContext:
    session_id: str = "test-session-01"
    workspace: str = "/tmp/workspace"
    provider_name: str = "anthropic"
    provider_model: str = "claude-sonnet-4-6"
    started_at: str = "2026-01-01T00:00:00Z"


@dataclass(frozen=True)
class StubBuildContext:
    sessions_dir: Path = field(default_factory=lambda: Path("/tmp/sessions"))
    session_id: str = "test-session-01"
    config_snapshot_yaml: str | None = None
    user_gate: Any = None
    bus: Any = None


@pytest.fixture
def bus() -> StubBus:
    return StubBus()


@pytest.fixture
def session_ctx() -> StubSessionContext:
    return StubSessionContext()


@pytest.fixture
def build_ctx(bus: StubBus) -> StubBuildContext:
    return StubBuildContext(bus=bus)


# ── Mini DB fixture ───────────────────────────────────────────────────────


def _ddl_full_schema() -> list[str]:
    """DDL that matches the real Briefbot schema for the columns we read."""
    return [
        """
        CREATE TABLE items (
            item_id TEXT NOT NULL PRIMARY KEY,
            dedupe_key TEXT NOT NULL UNIQUE,
            canonical_url TEXT,
            source_id TEXT NOT NULL,
            source_name TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            published_at TEXT,
            fetched_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            author TEXT,
            summary TEXT,
            tags_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            metrics_json TEXT,
            score REAL NOT NULL,
            source_category TEXT,
            source_tier INTEGER,
            source_max_daily INTEGER,
            watch_hits_json TEXT,
            score_opportunity REAL,
            opportunity_reason TEXT,
            opportunity_tags_json TEXT
        )
        """,
        """
        CREATE TABLE clusters (
            cluster_id TEXT NOT NULL PRIMARY KEY,
            label TEXT,
            created_at TEXT NOT NULL,
            first_seen_at TEXT,
            last_seen_at TEXT,
            item_count INTEGER NOT NULL DEFAULT 0,
            sources_count INTEGER NOT NULL DEFAULT 0,
            categories TEXT,
            top_tokens TEXT,
            velocity_7d INTEGER NOT NULL DEFAULT 0,
            velocity_3d INTEGER NOT NULL DEFAULT 0,
            velocity_1d INTEGER NOT NULL DEFAULT 0,
            diversity_score REAL NOT NULL DEFAULT 0.0,
            trend_score REAL NOT NULL DEFAULT 0.0,
            representative_url TEXT,
            representative_title TEXT
        )
        """,
        """
        CREATE TABLE topic_profiles (
            topic_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            first_seen_at TEXT,
            last_seen_at TEXT,
            count_1d INTEGER DEFAULT 0,
            count_3d INTEGER DEFAULT 0,
            count_7d INTEGER DEFAULT 0,
            count_30d INTEGER DEFAULT 0,
            momentum REAL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ]


def _build_mini_db(path: Path) -> None:
    """Create a small but representative Briefbot DB for tests.

    Contents (curated so assertions in the test files have known shape):
      - 6 items spanning ai_research, devtools, security, papers
      - 1 item just inside the 30-day window; 1 well past
      - 3 clusters with varied velocity profiles
      - 4 topics (2 entities, 2 tokens) with descending momentum
    """
    now = datetime.now(timezone.utc)
    iso = lambda offset_days: (now - timedelta(days=offset_days)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731

    conn = sqlite3.connect(str(path))
    try:
        for ddl in _ddl_full_schema():
            conn.execute(ddl)

        items = [
            # (item_id, title, url, summary, source_name, source_category,
            #  score, score_opportunity, opportunity_reason, fetched_offset_days)
            ("itm_001", "Ghidra script API tour", "https://example.org/ghidra-api",
             "A deep dive into the Ghidra script API and headless analyzer.",
             "Decompiler Blog", "devtools", 0.91, 0.85,
             "Useful for RE pipelines.", 1),
            ("itm_002", "Transformer attention revisited", "https://example.org/attn",
             "Survey of attention variants in modern transformers.",
             "arXiv", "ai_research", 0.88, None, None, 2),
            ("itm_003", "CVE-2026-0001 in libfoo", "https://example.org/cve",
             "Heap overflow in libfoo's frame parser.",
             "Project Zero", "security", 0.95, 0.92,
             "Active exploitation observed.", 3),
            ("itm_004", "Paper: scaling laws for tiny models", "https://example.org/scaling",
             "New scaling-laws regime under 1B parameters.",
             "Papers with Code", "papers", 0.80, None, None, 4),
            ("itm_005", "Sourcegraph adds Ghidra plugin", "https://example.org/sg-ghidra",
             "Sourcegraph IDE integrates with Ghidra workspaces.",
             "Sourcegraph Blog", "devtools", 0.65, None, None, 5),
            ("itm_006", "Ancient news no one wants", "https://example.org/old",
             "Should never show up in 30-day searches.",
             "Old Blog", "tech_news", 0.50, None, None, 90),
        ]
        for (
            item_id, title, url, summary, source_name, source_category,
            score, opp_score, opp_reason, fetched_days,
        ) in items:
            conn.execute(
                """
                INSERT INTO items (
                    item_id, dedupe_key, canonical_url, source_id, source_name,
                    title, url, published_at, fetched_at, last_seen_at,
                    author, summary, tags_json, raw_json, metrics_json, score,
                    source_category, source_tier, source_max_daily, watch_hits_json,
                    score_opportunity, opportunity_reason, opportunity_tags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id, item_id, url, source_name.lower().replace(" ", "_"),
                    source_name, title, url, iso(fetched_days), iso(fetched_days),
                    iso(fetched_days),
                    None, summary, json.dumps(["ai", "ml"]),
                    "{}", None, score,
                    source_category, 1, 100, None,
                    opp_score, opp_reason, None,
                ),
            )

        clusters = [
            # (cluster_id, label, item_count, v1d, v3d, v7d, trend, rep_title, rep_url)
            ("clu_001", "Ghidra tooling renaissance", 5, 1, 4, 8, 0.82,
             "Ghidra script API tour", "https://example.org/ghidra-api"),
            ("clu_002", "Attention research wave", 12, 3, 7, 18, 0.78,
             "Transformer attention revisited", "https://example.org/attn"),
            ("clu_003", "libfoo vulns", 2, 0, 2, 3, 0.55,
             "CVE-2026-0001 in libfoo", "https://example.org/cve"),
        ]
        for cid, label, count, v1, v3, v7, trend, rt, ru in clusters:
            conn.execute(
                """
                INSERT INTO clusters (
                    cluster_id, label, created_at, first_seen_at, last_seen_at,
                    item_count, sources_count, categories, top_tokens,
                    velocity_7d, velocity_3d, velocity_1d,
                    diversity_score, trend_score,
                    representative_url, representative_title
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (cid, label, iso(7), iso(7), iso(0), count, 3, "devtools", "",
                 v7, v3, v1, 0.5, trend, ru, rt),
            )

        topics = [
            # (topic_id, name, kind, c1, c3, c7, c30, momentum)
            ("top_001", "ghidra",        "entity", 1, 4, 8,  20, 0.92),
            ("top_002", "transformer",   "token",  3, 7, 18, 60, 0.85),
            ("top_003", "libfoo",        "entity", 0, 2, 3,  5,  0.55),
            ("top_004", "scaling laws",  "token",  0, 0, 1,  4,  0.10),
        ]
        for tid, name, kind, c1, c3, c7, c30, mom in topics:
            conn.execute(
                """
                INSERT INTO topic_profiles (
                    topic_id, name, kind, first_seen_at, last_seen_at,
                    count_1d, count_3d, count_7d, count_30d, momentum,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tid, name, kind, iso(7), iso(0), c1, c3, c7, c30, mom,
                 iso(7), iso(0)),
            )

        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def mini_db_path(tmp_path: Path) -> Path:
    """A freshly-built mini Briefbot DB for one test."""
    p = tmp_path / "mini.db"
    _build_mini_db(p)
    return p


@pytest.fixture
def mini_db_conn(mini_db_path: Path):
    """A read-only connection to mini.db. Closed automatically after the test."""
    conn = sqlite3.connect(
        f"file:{mini_db_path}?mode=ro", uri=True, check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
