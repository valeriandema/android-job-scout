"""
Ashby scraper — fetches jobs from each company on the curated list.

Ashby exposes a public, unauthenticated JSON board API:
  https://api.ashbyhq.com/posting-api/job-board/<slug>

Response shape: {"jobs": [...], "apiVersion": "..."} where each job has
`title`, `location`, `secondaryLocations`, `descriptionPlain`, `jobUrl`,
`publishedAt`, `isRemote`, `workplaceType`, etc. We take descriptionPlain
directly (already stripped of HTML) and concatenate the primary location
with any secondary locations so the downstream remote/EU filter sees all
geographic signals.
"""
from __future__ import annotations

import logging
import os
from typing import Iterator

import yaml

from ..matching import Job, extract_salary_usd
from .base import http_get

log = logging.getLogger(__name__)

API_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
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
        log.warning("ashby: companies.yaml missing at %s", COMPANIES_PATH)
        return []
    return list(data.get("ashby") or [])


def _combined_location(row: dict) -> str:
    parts: list[str] = []
    primary = (row.get("location") or "").strip()
    if primary:
        parts.append(primary)
    for sec in row.get("secondaryLocations") or []:
        name = (sec.get("location") or sec.get("name") or "").strip()
        if name:
            parts.append(name)
    if row.get("isRemote") and not any("remote" in p.lower() for p in parts):
        parts.append("Remote")
    return " · ".join(parts) or "Remote"


def _fetch_one(slug: str, cfg: dict) -> Iterator[Job]:
    r = http_get(API_URL.format(slug=slug), cfg,
                 headers={"Accept": "application/json"})
    if not r:
        log.info("ashby: %s -> no response (slug may be stale)", slug)
        return
    try:
        data = r.json()
    except Exception as e:
        log.warning("ashby: %s json parse failed: %s", slug, e)
        return

    for row in data.get("jobs", []) or []:
        if not row.get("isListed", True):
            continue
        title = (row.get("title") or "").strip()
        if not title:
            continue
        if not any(h in title.lower() for h in _TITLE_HINTS):
            continue
        description = (row.get("descriptionPlain") or "").strip()
        location = _combined_location(row)
        sm, sx = extract_salary_usd(f"{title} {description}")
        yield Job(
            source="ashby",
            id=f"{slug}:{row.get('id')}",
            title=title,
            company=slug,
            description=description,
            location=location,
            url=row.get("jobUrl") or row.get("applyUrl") or "",
            salary_min=sm,
            salary_max=sx,
            posted_at=row.get("publishedAt"),
        )


def fetch(cfg: dict) -> Iterator[Job]:
    slugs = _load_slugs()
    log.info("ashby: scanning %d companies", len(slugs))
    for slug in slugs:
        try:
            yield from _fetch_one(slug, cfg)
        except Exception as e:
            log.warning("ashby: %s crashed: %s", slug, e)
