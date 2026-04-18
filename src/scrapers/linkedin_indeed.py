"""
Best-effort LinkedIn / Indeed scraping.

Both block headless bots aggressively. This module therefore uses their
*public guest-search* endpoints (no auth) with realistic headers, and
returns what it can. When they return an empty or blocked response, we
just log and move on — the rest of the pipeline still yields value.

For production-grade LinkedIn/Indeed scraping you would use a paid API
(Bright Data, Scrapfly, etc.). We intentionally don't bake that in here.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator

from ..matching import Job, extract_salary_usd
from .base import http_get, html_to_text

log = logging.getLogger(__name__)


# ---------- LinkedIn ----------

LINKEDIN_GUEST = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    "?keywords=senior%20android%20engineer&f_WT=2&location=Worldwide&start=0"
)

_LI_CARD = re.compile(
    r'<a[^>]+href="(?P<url>https://www\.linkedin\.com/jobs/view/[^"?]+)[^"]*"[^>]*>.*?'
    r'<h3[^>]*class="[^"]*base-search-card__title[^"]*"[^>]*>(?P<title>[^<]+)</h3>.*?'
    r'<h4[^>]*class="[^"]*base-search-card__subtitle[^"]*"[^>]*>.*?>(?P<company>[^<]+)<.*?'
    r'<span[^>]*class="[^"]*job-search-card__location[^"]*"[^>]*>(?P<location>[^<]+)</span>',
    re.DOTALL,
)


def fetch_linkedin(cfg: dict) -> Iterator[Job]:
    r = http_get(LINKEDIN_GUEST, cfg, headers={
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    if not r:
        log.info("linkedin: no response (likely rate-limited) — skipping")
        return
    for m in _LI_CARD.finditer(r.text):
        title = m.group("title").strip()
        company = m.group("company").strip()
        loc = m.group("location").strip()
        url = m.group("url").strip()
        yield Job(
            source="linkedin",
            id=url,
            title=title,
            company=company,
            description=f"LinkedIn job card: {title} at {company}. View full description at {url}",
            location=loc,
            url=url,
            salary_min=None,
            salary_max=None,
            tags=[],
            posted_at=None,
        )


# ---------- Indeed ----------

INDEED_URL = "https://www.indeed.com/jobs?q=senior+android+engineer&l=remote&sort=date"

_IND_CARD = re.compile(
    r'<a[^>]+data-jk="(?P<jk>[A-Za-z0-9]+)"[^>]*>.*?'
    r'<span[^>]*title="(?P<title>[^"]+)"',
    re.DOTALL,
)


def fetch_indeed(cfg: dict) -> Iterator[Job]:
    r = http_get(INDEED_URL, cfg, headers={
        "Accept": "text/html",
        "Accept-Language": "en-US,en;q=0.9",
    })
    if not r:
        log.info("indeed: no response — skipping")
        return
    text = r.text
    for m in _IND_CARD.finditer(text):
        jk = m.group("jk")
        title = m.group("title").strip()
        yield Job(
            source="indeed",
            id=jk,
            title=title,
            company="",  # extracting company reliably from Indeed HTML is brittle
            description=f"Indeed listing: {title}. Full details at URL.",
            location="Remote",
            url=f"https://www.indeed.com/viewjob?jk={jk}",
            salary_min=None,
            salary_max=None,
            tags=[],
            posted_at=None,
        )


def fetch(cfg: dict) -> Iterator[Job]:
    yield from fetch_linkedin(cfg)
    yield from fetch_indeed(cfg)
