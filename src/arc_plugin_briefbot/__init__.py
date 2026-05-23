"""arc-plugin-briefbot — three read-only tools over a local Briefbot SQLite corpus.

Layout:
  plugin.py          — BriefbotPlugin + build() entry point
  dal.py             — ItemsDAL, ClustersDAL, TopicsDAL (sync sqlite3)
  tools/             — briefbot_search, briefbot_item, briefbot_trending

See _design/0001-briefbot-research-corpus.md for the design.
"""
__version__ = "0.1.0"
