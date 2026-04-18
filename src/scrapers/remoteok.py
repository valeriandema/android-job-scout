"""RemoteOK scraper — uses their public JSON API. Stable and generous."""
from __future__ import annotations

import logging
from typing import Iterator

from ..matching import Job, extract_salary_usd
from .base import http_get, html_to_text

log = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"


def fetch(cfg: dict) -> Iterator[Job]:
    r = http_get(API_URL, cfg, headers={"Accept": "application/json"})
    if not r:
        log.error("remoteok: failed to fetch")
        return
    try:
        data = r.json()
    except Exception as e:
        log.error("remoteok: json parse failed: %s", e)
        return

    # First element is metadata; the rest are job objects.
    for row in data:
        if not isinstance(row, dict) or "position" not in row:
            continue
        title = row.get("position", "") or row.get("title", "")
        if not title:
            continue
        description_html = row.get("description", "") or ""
        description = html_to_text(description_html)
        salary_min = row.get("salary_min")
        salary_max = row.get("salary_max")
        if not salary_max:
            # Fall back to text extraction
            sm, sx = extract_salary_usd(f"{title} {description}")
            salary_min = salary_min or sm
            salary_max = salary_max or sx
        yield Job(
            source="remoteok",
            id=str(row.get("id", row.get("slug", title))),
            title=title,
            company=row.get("company", "") or "",
            description=description,
            location=row.get("location", "") or "Remote",
            url=row.get("url") or f"https://remoteok.com/remote-jobs/{row.get('id', '')}",
            salary_min=salary_min,
            salary_max=salary_max,
            tags=row.get("tags", []) or [],
            posted_at=row.get("date"),
        )
