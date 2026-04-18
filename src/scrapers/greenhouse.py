"""
Greenhouse scraper — fetches jobs from each company on the curated list.

Greenhouse exposes a public, unauthenticated JSON board API:
  https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true

`content=true` returns the full HTML description so we can score on stack
keywords without a second request.
"""
from __future__ import annotations

import logging
import os
from typing import Iterator

import yaml

from ..matching import Job, extract_salary_usd
from .base import http_get, html_to_text

log = logging.getLogger(__name__)

API_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
COMPANIES_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "companies.yaml")
)

# Cheap pre-filter applied per-company so we don't yield obviously-irrelevant
# postings; the central matching.py still does the authoritative filtering.
_TITLE_HINTS = ("android", "mobile", "kotlin")


def _load_slugs() -> list[str]:
    try:
        with open(COMPANIES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("greenhouse: companies.yaml missing at %s", COMPANIES_PATH)
        return []
    return list(data.get("greenhouse") or [])


def _fetch_one(slug: str, cfg: dict) -> Iterator[Job]:
    r = http_get(API_URL.format(slug=slug), cfg,
                 params={"content": "true"},
                 headers={"Accept": "application/json"})
    if not r:
        log.info("greenhouse: %s -> no response (slug may be stale)", slug)
        return
    try:
        data = r.json()
    except Exception as e:
        log.warning("greenhouse: %s json parse failed: %s", slug, e)
        return

    for row in data.get("jobs", []) or []:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        if not any(h in title.lower() for h in _TITLE_HINTS):
            continue
        description = html_to_text(row.get("content", "") or "")
        loc = (row.get("location") or {}).get("name", "") or "Remote"
        sm, sx = extract_salary_usd(f"{title} {description}")
        yield Job(
            source="greenhouse",
            id=f"{slug}:{row.get('id')}",
            title=title,
            company=slug,
            description=description,
            location=loc,
            url=row.get("absolute_url") or "",
            salary_min=sm,
            salary_max=sx,
            posted_at=row.get("updated_at"),
        )


def fetch(cfg: dict) -> Iterator[Job]:
    slugs = _load_slugs()
    log.info("greenhouse: scanning %d companies", len(slugs))
    for slug in slugs:
        try:
            yield from _fetch_one(slug, cfg)
        except Exception as e:
            log.warning("greenhouse: %s crashed: %s", slug, e)
