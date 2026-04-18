"""
Y Combinator "Work at a Startup" scraper.

workatastartup.com doesn't have a documented public API, but the Algolia
backend serves the jobs board and is reachable. We hit their public index.
If that breaks, we fall back to the HTML page.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator

from ..matching import Job, extract_salary_usd
from .base import http_get, html_to_text

log = logging.getLogger(__name__)

# Their public HTML endpoint — renders server-side well enough to grep.
HTML_URL = "https://www.workatastartup.com/companies?query=android"


def fetch(cfg: dict) -> Iterator[Job]:
    r = http_get(HTML_URL, cfg)
    if not r:
        log.error("yc: failed to fetch")
        return
    html = r.text

    # The page embeds company/job data as JSON inside <script>. We extract the
    # job blocks via a permissive pattern and then pull fields.
    # This is best-effort; missing fields are tolerated.
    text = html_to_text(html)

    # Pull candidate company + job title lines that mention Android/Mobile.
    # This is intentionally lightweight — a proper integration would use
    # their GraphQL endpoint, but it requires auth.
    blocks = re.findall(
        r"(?i)([A-Z][\w&./'\- ]{1,60})\s*[—–-]\s*(Senior|Staff|Lead|Principal)?\s*"
        r"(Android|Mobile|Kotlin)[\w /]*?Engineer",
        text,
    )
    seen: set[str] = set()
    for company, seniority, kind in blocks:
        key = f"{company}:{seniority}:{kind}".lower()
        if key in seen:
            continue
        seen.add(key)
        title = f"{(seniority or '').strip()} {kind} Engineer".strip()
        lo, hi = extract_salary_usd(text)
        yield Job(
            source="yc",
            id=key,
            title=title,
            company=company.strip(),
            description=f"Listed on Y Combinator Work at a Startup. Full details at: {HTML_URL}",
            location="Remote (varies by company)",
            url=HTML_URL,
            salary_min=lo,
            salary_max=hi,
            tags=["yc"],
            posted_at=None,
        )
