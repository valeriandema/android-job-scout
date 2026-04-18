"""We Work Remotely scraper — parses their public RSS feed with lxml."""
from __future__ import annotations

import logging
from typing import Iterator

from lxml import etree

from ..matching import Job, extract_salary_usd
from .base import http_get, html_to_text

log = logging.getLogger(__name__)

FEED_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"


def _text(el, xpath: str, default: str = "") -> str:
    r = el.xpath(xpath)
    if not r:
        return default
    v = r[0]
    return (v.text if hasattr(v, "text") else str(v)) or default


def fetch(cfg: dict) -> Iterator[Job]:
    r = http_get(FEED_URL, cfg, headers={"Accept": "application/rss+xml, text/xml"})
    if not r:
        log.error("wwr: feed fetch failed")
        return
    try:
        root = etree.fromstring(r.content)
    except etree.XMLSyntaxError as e:
        log.error("wwr: feed parse failed: %s", e)
        return

    for item in root.iter("item"):
        title_raw = _text(item, "title/text()") or _text(item, "title")
        title_raw = (title_raw or "").strip()
        if ":" in title_raw:
            company, title = [x.strip() for x in title_raw.split(":", 1)]
        else:
            company, title = "", title_raw
        link = _text(item, "link/text()") or _text(item, "link")
        description = html_to_text(_text(item, "description/text()") or _text(item, "description"))
        # WWR puts region in <category>
        categories = [c.text for c in item.iter("category") if c.text]
        region = ""
        for c in categories:
            cl = c.lower()
            if any(k in cl for k in ("anywhere", "europe", "emea", "worldwide", "usa")):
                region = cl
                break
        lo, hi = extract_salary_usd(f"{title} {description}")
        guid = _text(item, "guid/text()") or _text(item, "guid") or link or title_raw
        yield Job(
            source="wwr",
            id=guid,
            title=title,
            company=company,
            description=description,
            location=region or "Remote",
            url=(link or "").strip(),
            salary_min=lo,
            salary_max=hi,
            tags=categories,
            posted_at=_text(item, "pubDate/text()") or _text(item, "pubDate"),
        )
