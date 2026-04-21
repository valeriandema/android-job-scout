"""
main.py — orchestrator.

Steps:
  1. Load config.
  2. Fan out to all scrapers, collect candidate jobs.
  3. Apply hard filters (title, location, salary).
  4. Score remaining jobs.
  5. Drop anything we've already seen.
  6. Take the top N.
  7. Format and push to Telegram (or dry-run print).
  8. Save "seen" state.

CLI:
  python -m src.main                 # normal run, uses env vars
  python -m src.main --dry-run       # skips Telegram, prints to stdout
  python -m src.main --no-state      # ignores seen.json (useful for debugging)
  python -m src.main --top 20        # override top_n
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from typing import Iterable

import yaml

from .llm_validator import LLMVerdictCache, validate_ranked
from .matching import Job, passes_filter, score_job
from .notifier import format_job, pack_into_messages, send
from .scrapers import (
    remoteok,
    remotive,
    weworkremotely,
    workingnomads,
    greenhouse,
    lever,
)
from .state import SeenStore

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")
STATE_PATH = os.path.join(REPO_ROOT, "data", "seen.json")
LLM_CACHE_PATH = os.path.join(REPO_ROOT, "data", "llm_verdicts.json")


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _load_cfg() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


SCRAPERS = [
    ("remoteok", remoteok.fetch),
    ("remotive", remotive.fetch),
    ("wwr", weworkremotely.fetch),
    ("workingnomads", workingnomads.fetch),
    ("greenhouse", greenhouse.fetch),
    ("lever", lever.fetch),
]


def collect_jobs(cfg: dict) -> list[Job]:
    jobs: list[Job] = []
    for name, fn in SCRAPERS:
        try:
            batch = list(fn(cfg))
            logging.info("scraper %s returned %d jobs", name, len(batch))
            jobs.extend(batch)
        except Exception as e:
            logging.exception("scraper %s crashed: %s", name, e)
    return jobs


def pipeline(cfg: dict, *, use_state: bool, top_n: int) -> tuple[list[dict], int, int]:
    """
    Returns (ranked_top_jobs_metadata, total_collected, total_after_filters).
    Each item in the list is: {job: Job, score: int, reasons: [str]}
    """
    all_jobs = collect_jobs(cfg)
    collected = len(all_jobs)

    # Dedup within-run by uniq_key (same job appears in multiple feeds).
    by_key: dict[str, Job] = {}
    for j in all_jobs:
        k = j.uniq_key()
        if k not in by_key:
            by_key[k] = j

    # Apply filters.
    passing: list[Job] = []
    for j in by_key.values():
        ok, _ = passes_filter(j, cfg)
        if ok:
            passing.append(j)
    after_filter = len(passing)

    # Score & sort.
    scored = []
    for j in passing:
        s, reasons = score_job(j, cfg)
        scored.append({"job": j, "score": s, "reasons": reasons})
    scored.sort(key=lambda x: -x["score"])

    # Filter out jobs we already notified about (unless --no-state).
    if use_state:
        store = SeenStore(STATE_PATH, cfg["dedup_days"])
        unseen = [x for x in scored if not store.has(x["job"].uniq_key())]
    else:
        store = None
        unseen = scored

    # LLM eligibility gate: walks the ranked list top-down, asks the model
    # whether each posting is genuinely open to EU/Bulgaria/Ukraine-based
    # candidates, skips the ones it can't prove eligible. Verdicts are cached
    # on disk so we don't burn API calls on the same job across runs.
    llm_cache: LLMVerdictCache | None = None
    if cfg.get("llm_validation", {}).get("enabled"):
        llm_cache = LLMVerdictCache(LLM_CACHE_PATH, cfg["dedup_days"])
    validated = validate_ranked(unseen, cfg, cache=llm_cache, need=top_n)
    if llm_cache is not None:
        llm_cache.save()

    top = validated[:top_n]

    # Mark them as seen ONLY now, so a failed Telegram send doesn't "lose" them.
    if store is not None:
        for x in top:
            store.mark(x["job"].uniq_key())
        store.save()

    return top, collected, after_filter


def build_messages(top: list[dict], collected: int, after_filter: int, cfg: dict) -> list[str]:
    date_str = dt.datetime.utcnow().strftime("%Y-%m-%d")
    if not top:
        header = (
            f"🤖 <b>Android Job Scout</b> — {date_str}\n"
            f"Scanned {collected} listings, {after_filter} matched hard filters. "
            f"No new matches since last run."
        )
        return [header]

    header = (
        f"🤖 <b>Android Job Scout</b> — {date_str}\n"
        f"Top <b>{len(top)}</b> new Senior/Lead Android roles "
        f"(≥${cfg['min_salary_usd']//1000}k, remote-friendly).\n"
        f"<i>Scanned {collected} listings · {after_filter} passed filters</i>"
    )

    blocks: list[str] = []
    for item in top:
        j: Job = item["job"]
        blocks.append(format_job(j, item["score"], item["reasons"],
                                 llm_verdict=item.get("llm_verdict")))

    footer = "— end —"
    return pack_into_messages(header, blocks, footer)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print to stdout instead of Telegram")
    parser.add_argument("--no-state", action="store_true", help="ignore seen.json (for debug)")
    parser.add_argument("--top", type=int, default=None, help="override top_n from config")
    args = parser.parse_args(argv)

    cfg = _load_cfg()
    top_n = args.top or cfg.get("top_n", 10)

    top, collected, after_filter = pipeline(cfg, use_state=not args.no_state, top_n=top_n)

    logging.info("pipeline: collected=%d, after_filter=%d, top=%d",
                 collected, after_filter, len(top))

    messages = build_messages(top, collected, after_filter, cfg)
    ok = send(messages, dry_run=args.dry_run)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
