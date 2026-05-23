# arc-plugin-briefbot

Out-of-tree arc plugin: three read-only tools over a local Briefbot SQLite
corpus. Forked from [`arc-plugin-template`](../arc-plugin-template).

## What's here

```
src/arc_plugin_briefbot/
  plugin.py             BriefbotPlugin (session-scoped DB owner) + build()
  dal.py                ItemsDAL, ClustersDAL, TopicsDAL, check_schema()
  tools/
    briefbot_search.py
    briefbot_item.py
    briefbot_trending.py
tests/
  conftest.py           StubBus, StubBuildContext, mini_db_path fixture
  test_dal.py           DAL queries against the mini DB
  test_plugin.py        Lifecycle (open, schema check, close, disabled paths)
  test_tools.py         Tool I/O, validation, event emission
_design/
  0001-briefbot-research-corpus.md
```

## arc plugin API contract

Targets **`arc.plugin_api` v0.1**. Only imports from there:

```python
from arc.plugin_api import (
    PluginBuildContext, RuntimeEvent, SessionContext, Tool, ToolError,
    ToolInputSchema, TurnOutcome,
)
```

Plugin shape: **session-scoped state + tools** (vs. websearch's stateless
shape). Lifecycle:

1. `build(config, build_ctx)` (entry point) — construct the plugin with
   config + bus, no DB work yet
2. `on_session_start(ctx)` — resolve DB path, open read-only, schema check
   via column probe (see §"Schema check"), build DAL + tools, emit
   `briefbot.ready`
3. `provides_tools()` — return the three tools (arc merges into registry)
4. tool calls → `briefbot.query` events with timing
5. `on_session_end(ctx, outcome)` — close the connection

## Schema check

The user's DB has `PRAGMA user_version = 0` (Briefbot doesn't set it) and
no `schema_migrations` table. So we **probe columns**, not versions:
`check_schema(conn)` runs `PRAGMA table_info(<table>)` and verifies every
column the DAL reads exists. Missing columns → `briefbot.schema_mismatch`
event, tools not registered, session continues.

## Path resolution (in `_resolve_db_path`)

1. `config.plugins.briefbot.config.db_path` if set
2. `$BRIEFBOT_DB_PATH` env var
3. `~/.briefbot/briefbot.db` if it exists (Briefbot's upstream default)
4. None → emit `briefbot.disabled`, no tools

**User's actual path** (not the default): `/Users/bubz/Developer/agent/projects/ai-assistant/data/briefbot.db`.
Set via env var.

## Testing

```bash
pip install -e ".[dev]"
pip install -e ../v2          # arc.plugin_api shim
pytest                          # 57 tests, ~0.5s, no network, no real DB
```

`mini_db_path` fixture builds a fresh in-memory-shaped DB from scratch
per test, mirroring the real schema. Update the fixture when upstream
Briefbot adds columns, not the assertions.

## Gotchas

- `hooks_order: {}` in arc's config works because arc auto-fills hook
  priorities for empty configs. Don't worry about pinning them.
- Tools use sync `sqlite3` (arc v2 has no ORM by choice — see arc's CLAUDE.md).
- The plugin opens with `?mode=ro&immutable=1` to coexist with Briefbot's
  ingestor; rare locked-file case surfaces as a transient `ToolError`.
- v1's Briefbot DAL is at `../v1/src/db/dal/briefbot/` — useful reference
  for SQL but DON'T import from it; this plugin is standalone.

## Common operations

- **Lift a query from v1:** read v1's SQLModel DAL → flatten to raw SQL →
  add to `src/arc_plugin_briefbot/dal.py` → add column(s) it reads to
  `REQUIRED_*_COLUMNS` so the schema check covers it.
- **Add a tool:** new file under `tools/`, implement Tool protocol, add to
  `BriefbotPlugin.on_session_start`'s tool list, write a test against the
  mini DB.
- **Smoke against real DB:** `BRIEFBOT_DB_PATH=... python3 -c "from
  arc_plugin_briefbot.plugin import build; ..."` then call tools directly.
- **Verify arc picks it up:** `python3 -c "import arc.plugins;
  arc.plugins._refresh_builders(); print(arc.plugins.last_discovery().discovered)"`
