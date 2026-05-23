# arc-plugin-briefbot

Three read-only tools that let arc query a local Briefbot research
corpus: a nightly-indexed SQLite database of AI/ML papers, blog posts,
HN/Lobsters items, dev-tools, and security news.

This is a personal plugin — Briefbot is the user's own machine-local
corpus, so this package is **not part of arc itself**. Install it
explicitly when you want it.

## What it does

| Tool | What it does |
|---|---|
| `briefbot_search`   | Title + summary search over the corpus, filterable by recency window and source category. |
| `briefbot_item`     | Fetch full details on a specific item by id. |
| `briefbot_trending` | Trending storyline clusters and hot topics (no query needed). |

All three are read-only; the plugin opens the SQLite file in
`mode=ro&immutable=1`.

## Install

```bash
pip install arc-plugin-briefbot
```

Point the plugin at your Briefbot DB (one of):

```bash
# Option 1: env var (matches v1)
export BRIEFBOT_DB_PATH=/path/to/briefbot.db

# Option 2: explicit in ~/.arc/config.yml under the plugin's config block
plugins:
  enabled:
    - name: briefbot
      enabled: true
      config:
        db_path: /path/to/briefbot.db
```

On the next `arc` launch, the first-run enablement flow detects the new
plugin and asks once:

```
[+] new arc plugin discovered: briefbot (from arc-plugin-briefbot v0.1.0)
    enable it for this and future sessions? [Y/n] Y
```

After that the plugin loads automatically each session. Flip it on or
off any time with `arc plugins`.

## Config

All keys optional:

```yaml
plugins:
  enabled:
    - name: briefbot
      enabled: true
      config:
        db_path: null                     # null → env / default path
        disabled_tools: []                # e.g. [briefbot_trending] to drop one
        search_default_days: 30           # default recency window for search
        search_default_limit: 15          # default result count
        trending_default_window: "3d"     # '1d' | '3d' | '7d'
        max_summary_chars: 1200           # truncate per-item summary
```

### DB path resolution

1. `config.plugins.briefbot.config.db_path` if set
2. `$BRIEFBOT_DB_PATH` if set
3. `~/.briefbot/briefbot.db` if it exists
4. None → plugin runs in disabled mode (emits `briefbot.disabled`,
   contributes no tools)

## Observability

The plugin and its tools emit structured events on arc's bus, so every
query lands in `events.jsonl` and `session.log`:

```
briefbot.ready             path, item_count, tools
briefbot.disabled          reason, path
briefbot.schema_mismatch   path, missing_columns
briefbot.query             tool, query/params, result_count, took_ms
```

Replay works without re-hitting the DB — arc captures tool inputs and
outputs verbatim.

## Failure modes

| What happens | Behavior |
|---|---|
| `db_path` unresolved or file missing | Emits `briefbot.disabled`; no tools; session continues |
| DB present but unreadable | Emits `briefbot.disabled` with the sqlite error; same outcome |
| DB schema is missing columns we read | Emits `briefbot.schema_mismatch`; no tools; session continues |
| Briefbot ingestor writing while you read | `?mode=ro&immutable=1` keeps reads consistent; rare locked-file case surfaces as a `ToolError` you can retry |
| Tool execution raises | Standard `ToolError` path; model sees it and adapts |

Briefbot being unavailable should never kill a session — it's additive.

## Development

```bash
git clone https://github.com/.../arc-plugin-briefbot
cd arc-plugin-briefbot
pip install -e ".[dev]"
pip install -e /path/to/arc/v2     # for `from arc.plugin_api import ...`
pytest
```

The test suite builds a small in-memory Briefbot DB from a known fixture
and exercises every DAL query, the plugin lifecycle, and each tool's
input validation + output rendering. 57 tests, ~0.5s, no network.

## Design

See [`_design/0001-briefbot-research-corpus.md`](_design/0001-briefbot-research-corpus.md)
for the full design rationale: why session-scoped lifecycle, why sync
sqlite3 instead of an ORM, what the schema-compatibility check verifies,
and what's deferred to future phases.

## License

MIT.
