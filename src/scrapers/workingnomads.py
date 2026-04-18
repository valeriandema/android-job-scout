"""Working Nomads scraper — public JSON feed, heavy on EU-friendly remote roles."""
from __future__ import annotations

import logging
from typing import Iterator

from ..matching import Job, extract_salary_usd
from .base import http_get, html_to_text

log = logging.getLogger(__name__)

API_URL = "https://www.workingnomads.com/api/exposed_jobs/"


def fetch(cfg: dict) -> Iterator[Job]:
    r = http_get(API_URL, cfg, headers={"Accept": "application/json"})
    if not r:
        log.error("workingnomads: failed")
        return
    try:
        rows = r.json()
    except Exception as e:
        log.error("workingnomads: parse %s", e)
        return
    for row in rows:
        title = row.get("title", "")
        description = html_to_text(row.get("description", ""))
        tags = (row.get("category_name", "") or "").split(",")
        lo, hi = extract_salary_usd(description)
        yield Job(
            source="workingnomads",
            id=str(row.get("slug") or row.get("url")),
            title=title,
            company=row.get("company_name", ""),
            description=description,
            location=row.get("region", "") or "Remote",
            url=row.get("url", ""),
            salary_min=lo,
            salary_max=hi,
            tags=[t.strip() for t in tags if t.strip()],
            posted_at=row.get("pub_date"),
        )
