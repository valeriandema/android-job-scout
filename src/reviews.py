"""
reviews.py — pull employee/company feedback signals from public sources.

We use two sources that reliably serve JSON to GitHub Actions runners:

1. Reddit JSON search (no auth required for basic listings)
   — surfaces posts like "working at X", "X review", etc.
2. HackerNews Algolia API
   — surfaces HN comments mentioning the company, often with
     first-hand comp, culture, and engineering-quality signal.

Returned data is designed to be pasted into a Telegram message, so we keep
snippets short and provide a link to the full source.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

from .scrapers.base import http_get

log = logging.getLogger(__name__)


@dataclass
class ReviewSnippet:
    source: str       # "reddit" | "hn"
    title: str
    url: str
    excerpt: str
    score: Optional[int] = None   # reddit upvotes / hn points
    subreddit_or_tag: Optional[str] = None


# --------------------------- Reddit ------------------------------------

REDDIT_SEARCH = "https://www.reddit.com/search.json"


def _fetch_reddit(company: str, cfg: dict, limit: int = 4) -> list[ReviewSnippet]:
    if not company:
        return []
    # Reddit requires a User-Agent other than default python-requests.
    queries = [
        f'"{company}" (review OR "working at" OR salary OR glassdoor OR interview)',
    ]
    results: list[ReviewSnippet] = []
    for q in queries:
        r = http_get(REDDIT_SEARCH, cfg, params={
            "q": q, "sort": "relevance", "limit": limit, "t": "year",
            "restrict_sr": "false", "type": "link",
        }, headers={"Accept": "application/json"})
        if not r:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        for child in data.get("data", {}).get("children", []) or []:
            d = child.get("data", {}) or {}
            title = d.get("title", "")
            selftext = d.get("selftext", "") or ""
            excerpt = (selftext or title).replace("\n", " ").strip()
            if len(excerpt) > 240:
                excerpt = excerpt[:237] + "..."
            results.append(ReviewSnippet(
                source="reddit",
                title=title[:160],
                url=f"https://www.reddit.com{d.get('permalink', '')}",
                excerpt=excerpt,
                score=d.get("score"),
                subreddit_or_tag=d.get("subreddit"),
            ))
    # Dedup by URL, keep highest-scored first
    dedup: dict[str, ReviewSnippet] = {}
    for s in results:
        existing = dedup.get(s.url)
        if not existing or (s.score or 0) > (existing.score or 0):
            dedup[s.url] = s
    return sorted(dedup.values(), key=lambda s: -(s.score or 0))[:limit]


# --------------------------- HackerNews --------------------------------

HN_SEARCH = "https://hn.algolia.com/api/v1/search"


def _fetch_hn(company: str, cfg: dict, limit: int = 4) -> list[ReviewSnippet]:
    if not company:
        return []
    r = http_get(HN_SEARCH, cfg, params={
        "query": company,
        "tags": "comment",
        "hitsPerPage": limit * 2,  # we'll filter
    }, headers={"Accept": "application/json"})
    if not r:
        return []
    try:
        hits = r.json().get("hits", []) or []
    except Exception:
        return []
    company_lc = company.lower()
    results: list[ReviewSnippet] = []
    for h in hits:
        text = re.sub(r"<[^>]+>", "", h.get("comment_text", "") or "").strip()
        if company_lc not in text.lower():
            continue
        excerpt = text.replace("\n", " ")
        if len(excerpt) > 240:
            excerpt = excerpt[:237] + "..."
        results.append(ReviewSnippet(
            source="hn",
            title=h.get("story_title", "")[:160],
            url=f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            excerpt=excerpt,
            score=h.get("points"),
            subreddit_or_tag="hn",
        ))
    return results[:limit]


# --------------------------- Public ------------------------------------

def fetch_reviews(company: str, cfg: dict) -> list[ReviewSnippet]:
    """Return a merged list of Reddit + HN snippets for the given company."""
    if not company or len(company) < 2:
        return []
    try:
        reddit = _fetch_reddit(company, cfg, limit=3)
    except Exception as e:
        log.warning("reviews: reddit failed for %s: %s", company, e)
        reddit = []
    try:
        hn = _fetch_hn(company, cfg, limit=3)
    except Exception as e:
        log.warning("reviews: hn failed for %s: %s", company, e)
        hn = []
    return reddit + hn
