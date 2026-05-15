"""Web search + scrape for the agent team.

Without this module the team has no way to learn anything outside its
training cutoff — no current KDP categories, no Etsy fee changes, no
trending niche signal, no competitor research. Agent products without
search are guess machines.

Today's contract:

- ``web_search(query, max_results=5)`` returns a list of
  :class:`SearchResult` (title / url / snippet).
- Backends in priority order: Brave (key) → Tavily (key) →
  Exa (key) → DDG (free, no key). First available wins.
- Returns ``[]`` on any error — caller decides whether to retry or
  ship without research. Never raises; never blocks the agent loop.

Scoped intentionally narrow for v1: search only. Content extraction
(scraping article bodies) is a follow-up.
"""
from korpha.web.search import SearchResult, web_search

__all__ = ["SearchResult", "web_search"]
