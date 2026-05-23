# 0001 — Briefbot research-corpus plugin

## Motivation

Briefbot is a nightly-indexed local SQLite corpus of AI/ML papers, blog
posts, HN/Lobsters items, dev-tools and security news, sitting at
`/Users/bubz/Developer/agent/projects/ai-assistant/data/briefbot.db`. It's the user's research surface — "what's the
state of <tech>", "find a paper about X", "summarize this week's tooling
activity" — and the agent leans on it constantly.

The corpus is personal. Most arc users don't have it; some probably
shouldn't. Bundling it into arc's main repo would force a dependency and a
DB-path concept on people who don't want either, and exposing the corpus
schema in a public repo creates an awkward upstream-versus-local
maintenance split. The right shape is an external pip-installable plugin
that the user opts into: `pip install arc-plugin-briefbot`, arc's first-run
enablement prompt asks once, done.

This doc designs that plugin against arc's v0.1 plugin API.

## Scope

In:
- A single plugin (`name: briefbot`) that:
  - Opens the Briefbot SQLite file read-only in `on_session_start`
  - Owns three tool instances bound to the DB handle
  - Closes the connection in `on_session_end`
  - Quarantines cleanly when the DB is missing/unreadable (tools simply
    aren't registered; session continues)
- Three tools — `briefbot_search`, `briefbot_item`, `briefbot_trending` —
  contributed via `provides_tools()`
- A tiny DAL layer (sync `sqlite3`) wrapping the three SQL queries
- Schema-version awareness: refuse to operate against a schema we don't
  know how to read; report it as `briefbot.schema_mismatch`
- Per-tool observability events: `briefbot.ready` / `briefbot.disabled` /
  `briefbot.schema_mismatch` / `briefbot.query`

Out (deferred, may live in a follow-up phase or sibling plugin):
- RAG over session chunks (v1's `LocalRagService` + LanceDB). Briefbot
  itself is read-only; RAG over arc's session corpus is a separate concern
  that belongs in arc's `0089` context-strategies phase, not here.
- Briefbot *ingestion*. arc is a consumer; Briefbot writes its own DB.
- Cross-session learning / persistent user notes on Briefbot items. Would
  fit a sibling `arc-plugin-briefbot-notes` plugin if we want it later.
- Briefbot items feeding back into a vector index (RAG follow-up above).

## The plugin API contract

This plugin targets **`arc.plugin_api` v0.1**. Specifically:

- Registered via the `arc.plugins` entry-point group (see `pyproject.toml`).
- Implements `on_session_start` and `on_session_end` for lifecycle.
- Implements `provides_tools()` to contribute the three tools after the DB
  handle is built.
- Implements `bind_bus(bus)` so the plugin and its tools can emit
  structured events for replay and `arc log`.
- Imports public types **only** from `arc.plugin_api`. No internal
  `arc.tools.base` or `arc.runtime.hooks` imports.

This is the "session-scoped state + tools" plugin shape — the briefbot
case is exactly the canonical use of `on_session_start` + `provides_tools`.

## Architecture

```
src/arc_plugin_briefbot/
  __init__.py
  plugin.py              ← Lifecycle owner; opens/closes DB; build() entry point
  dal.py                 ← ItemsDAL, ClustersDAL, TopicsDAL
  tools/
    __init__.py
    briefbot_search.py
    briefbot_item.py
    briefbot_trending.py
tests/
  conftest.py            ← StubBus/StubBuildContext + tests/fixtures/mini.db builder
  test_dal.py
  test_plugin.py
  test_tools.py
  fixtures/
    mini.db              ← tiny sqlite for unit tests (~20 items, 3 clusters)
_design/
  0001-briefbot-research-corpus.md
```

### Plugin class shape

```python
class BriefbotPlugin:
    name = "briefbot"

    def __init__(self, *, db_path: Path | None,
                 search_default_days: int, search_default_limit: int,
                 trending_default_window: str, max_summary_chars: int,
                 disabled_tools: list[str]):
        ...
        self._conn: sqlite3.Connection | None = None
        self._tools: list[Tool] = []
        self._bus = None

    def bind_bus(self, bus): self._bus = bus

    def on_session_start(self, ctx: SessionContext) -> None: ...
    def on_session_end(self, ctx, outcome) -> None: ...
    def provides_tools(self) -> list[Tool]: return list(self._tools)


def build(config: dict, build_ctx) -> BriefbotPlugin:
    """Entry point. Referenced from pyproject.toml."""
    return BriefbotPlugin(
        db_path=_resolve_db_path(config.get("db_path")),
        search_default_days=int(config.get("search_default_days", 30)),
        search_default_limit=int(config.get("search_default_limit", 15)),
        trending_default_window=str(config.get("trending_default_window", "3d")),
        max_summary_chars=int(config.get("max_summary_chars", 1200)),
        disabled_tools=list(config.get("disabled_tools", [])),
    )
```

### DB lifecycle

`on_session_start`:

1. Resolve `db_path` (see §"Config resolution order").
2. If unresolved/missing → emit `briefbot.disabled`, return. No tools
   registered; session continues normally.
3. `sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True,
   check_same_thread=False)`.
4. Set `row_factory = sqlite3.Row`.
5. Check schema version (see §"Schema version awareness"). On mismatch →
   emit `briefbot.schema_mismatch`, close the connection, return.
6. Build the three DAL instances, build the three tools, bind the bus to
   each tool that defines `bind_bus`.
7. Emit `briefbot.ready` with `item_count`, `tools`, `path`.

`on_session_end`:

- If `self._conn is not None`: close it. Idempotent.

### Schema-version awareness

Query `SELECT MAX(version) FROM schema_migrations` (or `PRAGMA
user_version` as a fallback) once at open. The plugin pins a
`MIN_SCHEMA_VERSION` constant; if the live schema is below that, fail
fast via `briefbot.schema_mismatch` and disable the tools.

This is deliberate: an out-of-date Briefbot has subtly different columns
(e.g. `score_opportunity` was added in vX), and silent column-missing
errors at query time would surface as cryptic `ToolError` messages mid-
turn. Better to refuse at session start.

### DAL design

Three small classes. Each takes a `sqlite3.Connection`, returns plain
dicts (not ORM models — arc has no ORM; the user already decided that).

```python
class ItemsDAL:
    def __init__(self, conn): self._c = conn

    def count(self) -> int: ...
    def search(self, *, query: str, days: int, category: str | None,
               limit: int, order_by: str) -> list[dict]: ...
    def get_by_id(self, item_id: str) -> dict | None: ...

class ClustersDAL:
    def get_trending(self, *, window: str, limit: int) -> list[dict]: ...

class TopicsDAL:
    def get_top_topics(self, *, window: str, limit: int) -> list[dict]: ...
```

SQL is lifted from v1's Briefbot DAL: LIKE on title + summary for items,
`velocity_*` ordering for clusters, momentum for topics. The DAL is the
boundary; tools call `dal.search(query=..., days=..., ...)` and never see
raw SQL.

## Tool surface (for the model)

### `briefbot_search`

```
name:        briefbot_search
description: Search the local Briefbot research corpus (papers, blogs,
             HN/Lobsters, tooling and security news, refreshed nightly).
             Use briefbot_item to drill into a result.
input:       query       (string, required)
             days        (int, default 30 — recency window)
             category    (enum, optional:
                          ai_research | papers | ai_industry | devtools |
                          mlops_infra | security | tech_news | aggregator)
             limit       (int, default 15, max 50)
             order_by    (enum: 'score'|'date', default 'score')
output:      ranked list:
               [1] (score=87) Title — source
                   https://url
                   summary excerpt...
                   opportunity: ...
```

### `briefbot_item`

```
name:        briefbot_item
description: Fetch full details for a Briefbot item by id.
input:       item_id (string, required)
output:      title, url, source, author, score / score_opportunity, tags,
             published_at / fetched_at, opportunity_reason, summary
```

### `briefbot_trending`

```
name:        briefbot_trending
description: Trending story clusters and hot topics in the corpus (no
             query needed). Use to discover what to read about.
input:       window         (enum: '1d'|'3d'|'7d', default '3d')
             clusters_limit (int, default 8)
             topics_limit   (int, default 10)
output:      Trending clusters (label, trend_score, velocity_*, item_count,
             representative title+URL) + hot topics (name, kind, momentum,
             counts across windows)
```

Output formatting follows v1's conventions — leading rank/score, then URL,
then summary. Easy for the model to parse and quote in a final response.

## Config

The plugin lives in **two** config surfaces:

1. **`pyproject.toml`** (this repo) declares the entry point and dependencies.
   Nothing user-facing.

2. **arc's `~/.arc/config.yml`** (the host) holds the per-user knobs. arc's
   first-run enablement flow writes the initial entry; the user edits from
   there.

   ```yaml
   plugins:
     enabled:
       # ... built-ins ...
       - name: briefbot
         enabled: true                       # toggle without uninstalling
         config:
           db_path: null                     # null → env / default path
           disabled_tools: []                # e.g. [briefbot_trending] to drop one
           search_default_days: 30
           search_default_limit: 15
           trending_default_window: "3d"
           max_summary_chars: 1200
         hooks_order:
           on_session_start: 30              # after recorder (10)
           on_session_end: 30
   ```

### Config resolution order for `db_path`

The DB path is resolved at `on_session_start` time:

1. `config.plugins.briefbot.config.db_path` if non-null
2. `$BRIEFBOT_DB_PATH` env var if set
3. `/Users/bubz/Developer/agent/projects/ai-assistant/data/briefbot.db` if it exists (the upstream default)
4. None → emit `briefbot.disabled`, no tools

This matches v1's resolution; moving it into the plugin (vs. a global
arc settings module) keeps the path concern contained.

## Install UX

```
$ pip install arc-plugin-briefbot
$ arc
[+] new arc plugin discovered: briefbot (from arc-plugin-briefbot v0.1.0)
    enable it for this and future sessions? [Y/n]
Y
arc › ...
```

arc's first-run enablement (host-side prep, 2026-05-23) handles the
prompt, persistence, and reload. If the user installs without launching
the TUI (e.g. plans to use `arc run` only), the plugin stays dormant
until they run `arc` once or toggle via `arc plugins`. Same flow on
uninstall: a dangling config entry shows up in `arc plugins` for cleanup.

## Observability

All events emitted by this plugin land in the session's `events.jsonl`
and the human-readable `session.log` via arc's generic-fallback
formatter. No formatter contribution from this plugin needed for v0.1.

```
briefbot.ready             { path, item_count, tools }
briefbot.disabled          { reason, path }
briefbot.schema_mismatch   { path, found_version, min_supported }
briefbot.query             { tool, query, params, result_count, took_ms }
```

`briefbot.query` emits per tool call. arc's standard
`tool.call.started/completed` already carries input + output; this event
adds structured query params and timing for performance visibility,
which don't fit naturally in the tool's returned string.

Log-writer fallback render (no per-plugin formatter required):

```
briefbot.ready  path=/Users/x/.briefbot/briefbot.db, item_count=12847, tools=['briefbot_search', ...]
briefbot.query  tool=briefbot_search, query='ghidra', result_count=8, took_ms=14
```

Suffices for v0.1. If we want pretty per-event renders later, that's a
future "plugin formatter entry-point group" enhancement on arc's side.

## Recovery and failure modes

| Failure | Behavior |
|---|---|
| `db_path` unresolved / file missing | Plugin emits `briefbot.disabled`; `provides_tools` returns `[]`; session continues normally. |
| DB present but unreadable | `sqlite3.OperationalError` → `briefbot.disabled` with the reason; same outcome. |
| Schema version below floor | `briefbot.schema_mismatch` event; connection closed; tools not registered. |
| Tool execution raises (corrupt row, encoding) | `ToolError` with the underlying message; model sees it; arc's tool-cycle detector handles retry storms. |
| Briefbot ingestor mid-write (rare with WAL) | `?mode=ro&immutable=1` rejects writers; if the file is genuinely being rewritten, surface as a transient `ToolError("briefbot DB locked; try again")`. |
| Empty result set | Tool returns `"No results for '<query>' in last <days>d"` as a *success* string — not an error. |
| Plugin construction raises | Standard arc plugin-quarantine path; session continues without briefbot. |

The "disabled, continue" path is deliberate. Briefbot is *additive*. Not
having it shouldn't kill a session.

## Test plan

### Unit — `test_dal.py` (against `tests/fixtures/mini.db`)

1. `ItemsDAL.search` — query match, days filter, category filter, limit,
   `order_by` score / date
2. `ItemsDAL.get_by_id` — hit + miss
3. `ClustersDAL.get_trending` — per-window ordering
4. `TopicsDAL.get_top_topics` — momentum ordering

### Unit — `test_tools.py`

1. Each tool's `input_schema` — required / optional fields, enum values
2. Each tool's `execute` against a stub DAL — well-formed output, empty-
   result string, `max_summary_chars` truncation
3. Each tool emits `briefbot.query` via the bound bus
4. Tool raises `ToolError` on DAL exception (corrupt fixture)

### Unit — `test_plugin.py`

1. Config resolution order for `db_path` (config > env > default > None)
2. Missing DB → `briefbot.disabled`, `provides_tools()` returns `[]`
3. Present DB → `briefbot.ready` with `item_count` + tool list
4. `disabled_tools: [briefbot_trending]` drops the named tool
5. `on_session_end` closes the connection (assert via `conn.in_transaction`
   or by re-opening read/write)
6. Schema mismatch emits `briefbot.schema_mismatch` and disables
7. `build()` plumbs the bus through to the plugin

### Integration — `test_briefbot_live.py`

Skip unless `BRIEFBOT_DB_PATH` is set and the file exists:

1. Run a turn that calls `briefbot_search`, assert non-empty result
2. Run a turn that calls `briefbot_trending`, assert clusters returned

### Smoke (manual, not in CI)

- `pip install -e .` in a venv with arc checked out
- `arc` — first-run prompt fires for "briefbot"; answer Y
- `arc run "any interesting AI papers this week?"` — confirms tool path
- Without `BRIEFBOT_DB_PATH`: same command runs, tool absent, agent
  answers honestly about not having Briefbot available
- `arc plugins` — briefbot appears in the toggle list

## State

Planned.
