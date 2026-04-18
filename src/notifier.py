"""
notifier.py — Telegram Bot API delivery.

Uses plain HTTPS to https://api.telegram.org/bot<token>/sendMessage.
Splits long digests into multiple messages so we never exceed Telegram's
4096-char limit. HTML parse mode is used (more forgiving escaping than MarkdownV2).
"""
from __future__ import annotations

import html
import logging
import os
import time
from typing import Optional

import requests

from .matching import Job
from .reviews import ReviewSnippet

log = logging.getLogger(__name__)

TG_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3800   # keep a safety margin below Telegram's 4096 ceiling


def _e(s: str) -> str:
    """HTML-escape for Telegram parse_mode=HTML."""
    return html.escape(s or "", quote=False)


def _format_salary(job: Job) -> str:
    if job.salary_min and job.salary_max:
        return f"${job.salary_min//1000}k–${job.salary_max//1000}k"
    if job.salary_max:
        return f"up to ${job.salary_max//1000}k"
    if job.salary_min:
        return f"${job.salary_min//1000}k+"
    return "salary not disclosed"


def format_job(job: Job, score: int, reasons: list[str], reviews: list[ReviewSnippet]) -> str:
    """Format a single job + its reviews as a Telegram HTML chunk."""
    head = (
        f"<b>{_e(job.title)}</b>\n"
        f"🏢 {_e(job.company or '—')}  ·  📍 {_e(job.location or 'Remote')}\n"
        f"💰 {_format_salary(job)}  ·  📊 score <b>{score}</b>  ·  <i>{_e(job.source)}</i>\n"
        f'🔗 <a href="{_e(job.url)}">Open job</a>'
    )

    if reasons:
        top_reasons = ", ".join(reasons[:4])
        head += f"\n🎯 <i>{_e(top_reasons)}</i>"

    if reviews:
        head += "\n\n<b>Company feedback</b>"
        for rv in reviews[:3]:
            icon = "🟠" if rv.source == "reddit" else "🟡"
            tag = _e(rv.subreddit_or_tag or rv.source)
            head += (
                f"\n{icon} <a href=\"{_e(rv.url)}\">[{tag}]</a> "
                f"{_e(rv.excerpt)}"
            )
    return head


def send(chunks: list[str], *, token: Optional[str] = None,
         chat_id: Optional[str] = None, dry_run: bool = False) -> bool:
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if dry_run or not token or not chat_id:
        log.info("notifier: dry-run (token/chat_id missing or dry_run=True)")
        for i, c in enumerate(chunks, 1):
            print(f"\n--- TELEGRAM MESSAGE {i}/{len(chunks)} ---\n{c}\n")
        return True

    url = TG_API.format(token=token)
    ok_count = 0
    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 200 and r.json().get("ok"):
                ok_count += 1
            else:
                log.error("telegram send failed: %s %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.error("telegram send raised: %s", e)
        time.sleep(0.6)  # be polite to the API
    return ok_count == len(chunks)


def pack_into_messages(header: str, blocks: list[str], footer: str = "") -> list[str]:
    """
    Given a list of pre-formatted job blocks, pack them into <=MAX_LEN messages.
    First message is prefixed with `header`, last is suffixed with `footer`.
    """
    if not blocks:
        msg = header
        if footer:
            msg += "\n\n" + footer
        return [msg]

    messages: list[str] = []
    current = header
    for b in blocks:
        candidate = current + "\n\n" + b if current else b
        if len(candidate) > MAX_LEN:
            messages.append(current)
            current = b
        else:
            current = candidate
    if current:
        messages.append(current)
    if footer:
        # Append footer to the last message if it fits, else new message.
        if len(messages[-1]) + len(footer) + 2 <= MAX_LEN:
            messages[-1] += "\n\n" + footer
        else:
            messages.append(footer)
    return messages
