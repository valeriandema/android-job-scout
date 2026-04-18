"""Shared HTTP + HTML helpers for scrapers."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("scrapers")


def http_get(url: str, cfg: dict, *, headers: Optional[dict] = None, params: Optional[dict] = None,
             retries: int = 2, backoff: float = 2.0) -> Optional[requests.Response]:
    final_headers = {"User-Agent": cfg["user_agent"], "Accept": "*/*"}
    if headers:
        final_headers.update(headers)
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=final_headers, params=params, timeout=cfg["http_timeout_sec"])
            if r.status_code == 200:
                return r
            log.warning("GET %s -> %s (attempt %d)", url, r.status_code, attempt + 1)
        except requests.RequestException as e:
            log.warning("GET %s raised %s (attempt %d)", url, e, attempt + 1)
        if attempt < retries:
            time.sleep(backoff * (attempt + 1))
    return None


def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    # Drop scripts/styles
    for s in soup(["script", "style"]):
        s.decompose()
    return " ".join(soup.get_text(separator=" ").split())
