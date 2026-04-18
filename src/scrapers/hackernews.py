"""
HackerNews "Who is hiring?" scraper.

Uses the HN Algolia API to find the current month's "Ask HN: Who is hiring?"
thread, then walks the direct top-level comments which are the individual job posts.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator, Optional

from ..matching import Job, extract_salary_usd
from .base import http_get, html_to_text

log = logging.getLogger(__name__)

ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"


def _find_latest_who_is_hiring(cfg: dict) -> Optional[int]:
    """Return the HN story ID of the most recent 'Who is hiring?' thread."""
    r = http_get(ALGOLIA_SEARCH, cfg, params={
        "query": "Ask HN: Who is hiring?",
        "tags": "story,author_whoishiring",
        "hitsPerPage": 5,
    })
    if not r:
        return None
    try:
        hits = r.json().get("hits", [])
    except Exception:
        return None
    # Pick the first hit that looks like "Ask HN: Who is hiring? (Month Year)"
    for hit in hits:
        title = hit.get("title", "")
        if "who is hiring" in title.lower():
            try:
                return int(hit.get("objectID"))
            except (TypeError, ValueError):
                continue
    return None


def _extract_company(text: str) -> str:
    # HN hiring posts usually start "Company | Role | Location..."
    head = text.splitlines()[0] if text else ""
    parts = [p.strip() for p in re.split(r"\||—|–|-", head) if p.strip()]
    return parts[0][:80] if parts else ""


def fetch(cfg: dict) -> Iterator[Job]:
    story_id = _find_latest_who_is_hiring(cfg)
    if not story_id:
        log.warning("hn: no 'Who is hiring?' thread found")
        return
    r = http_get(HN_ITEM.format(id=story_id), cfg)
    if not r:
        return
    try:
        story = r.json() or {}
    except Exception:
        return
    kids = story.get("kids", []) or []
    log.info("hn: walking %d top-level comments in story %d", len(kids), story_id)

    # Cap to keep the run fast in CI. The most relevant android posts usually
    # surface anywhere, so we walk all of them but bounded.
    for kid_id in kids[:600]:
        cr = http_get(HN_ITEM.format(id=kid_id), cfg, retries=1, backoff=1.0)
        if not cr:
            continue
        try:
            item = cr.json() or {}
        except Exception:
            continue
        if item.get("deleted") or item.get("dead"):
            continue
        text_html = item.get("text", "") or ""
        text = html_to_text(text_html)
        if not text:
            continue
        # Quick pre-filter so we don't pay salary regex cost for every post
        tl = text.lower()
        if "android" not in tl and "kotlin" not in tl and "mobile" not in tl:
            continue
        company = _extract_company(text)
        # HN comments don't have explicit titles. Use the first 120 chars as title.
        title_line = text.splitlines()[0] if text else text[:120]
        lo, hi = extract_salary_usd(text)
        yield Job(
            source="hn_hiring",
            id=str(item.get("id")),
            title=title_line[:160],
            company=company,
            description=text,
            location="",   # HN posts encode location in the title/body
            url=f"https://news.ycombinator.com/item?id={item.get('id')}",
            salary_min=lo,
            salary_max=hi,
            tags=[],
            posted_at=None,
        )
