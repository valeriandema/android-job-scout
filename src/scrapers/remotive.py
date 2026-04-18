"""Remotive scraper — stable public API at https://remotive.com/api/remote-jobs"""
from __future__ import annotations

import logging
from typing import Iterator

from ..matching import Job, extract_salary_usd
from .base import http_get, html_to_text

log = logging.getLogger(__name__)

API_URL = "https://remotive.com/api/remote-jobs"


def fetch(cfg: dict) -> Iterator[Job]:
    # Remotive supports ?category=software-dev&search=android but the combined
    # filter misses some; fetch software-dev and let matching.py decide.
    r = http_get(API_URL, cfg, params={"category": "software-dev", "limit": 250},
                 headers={"Accept": "application/json"})
    if not r:
        log.error("remotive: failed to fetch")
        return
    try:
        data = r.json()
    except Exception as e:
        log.error("remotive: json parse failed: %s", e)
        return

    for row in data.get("jobs", []):
        title = row.get("title", "")
        description = html_to_text(row.get("description", ""))
        salary_str = row.get("salary", "") or ""
        lo, hi = extract_salary_usd(f"{salary_str} {description}")
        yield Job(
            source="remotive",
            id=str(row.get("id")),
            title=title,
            company=row.get("company_name", ""),
            description=description,
            location=row.get("candidate_required_location", "") or "Remote",
            url=row.get("url", ""),
            salary_min=lo,
            salary_max=hi,
            tags=row.get("tags", []) or [],
            posted_at=row.get("publication_date"),
        )
