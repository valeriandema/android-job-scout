"""
Lever scraper — fetches jobs from each company on the curated list.

Lever exposes a public, unauthenticated postings API:
  https://api.lever.co/v0/postings/<slug>?mode=json

Returns the full posting (descriptionPlain, lists, etc.) so a single request
per company is enough.
"""
from __future__ import annotations

import logging
import os
from typing import Iterator

import yaml

from ..matching import Job, extract_salary_usd
from .base import http_get

log = logging.getLogger(__name__)

API_URL = "https://api.lever.co/v0/postings/{slug}"
COMPANIES_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "companies.yaml")
)

_TITLE_HINTS = ("android", "mobile", "kotlin")


def _load_slugs() -> list[str]:
    try:
        with open(COMPANIES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("lever: companies.yaml not found at %s", COMPANIES_PATH)
        return []
    return list(data.get("lever") or [])


def _flatten_lists(lists: list) -> str:
    """Lever splits descriptions into 'lists' of {text, content} dicts."""
    out: list[str] = []
    for item in lists or []:
        if not isinstance(item, dict):
            continue
        if item.get("text"):
            out.append(str(item["text"]))
        if item.get("content"):
            out.append(str(item["content"]))
    return " ".join(out)


def _fetch_one(slug: str, cfg: dict) -> Iterator[Job]:
    r = http_get(API_URL.format(slug=slug), cfg,
                 params={"mode": "json"},
                 headers={"Accept": "application/json"})
    if not r:
        log.info("lever: %s -> no response (likely slug renamed/migrated)", slug)
        return
    try:
        rows = r.json()
    except Exception as e:
        log.warning("lever: %s json parse failed: %s", slug, e)
        return

    for row in rows or []:
        title = (row.get("text") or "").strip()
        if not title:
            continue
        if not any(h in title.lower() for h in _TITLE_HINTS):
            continue
        description = " ".join([
            row.get("descriptionPlain", "") or "",
            _flatten_lists(row.get("lists", [])),
            row.get("additionalPlain", "") or "",
        ]).strip()
        cats = row.get("categories", {}) or {}
        loc = cats.get("location", "") or cats.get("allLocations", [""])[0] or "Remote"
        sm, sx = extract_salary_usd(f"{title} {description}")
        yield Job(
            source="lever",
            id=f"{slug}:{row.get('id')}",
            title=title,
            company=slug,
            description=description,
            location=loc,
            url=row.get("hostedUrl") or row.get("applyUrl") or "",
            salary_min=sm,
            salary_max=sx,
            tags=[cats.get("team", ""), cats.get("commitment", "")],
            posted_at=str(row.get("createdAt", "")),
        )


def fetch(cfg: dict) -> Iterator[Job]:
    slugs = _load_slugs()
    log.info("lever: scanning %d companies", len(slugs))
    for slug in slugs:
        try:
            yield from _fetch_one(slug, cfg)
        except Exception as e:
            log.warning("lever: %s crashed: %s", slug, e)
