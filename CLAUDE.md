# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Local dev (from the `android-job-scout/` directory):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Dry-run: prints Telegram messages to stdout, skips state writes (via --no-state)
python -m src.main --dry-run --no-state --top 5

# Real send (needs env vars)
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python -m src.main --top 10
```

CLI flags on `src.main`: `--dry-run` (no Telegram call), `--no-state` (ignore `data/seen.json`), `--top N` (override `top_n` from config).

Tests — there is no pytest setup; the test file is a plain script that prints PASS/FAIL:

```bash
python -m tests.test_pipeline          # runs all offline checks
```

To run a single check, call its function directly, e.g.:

```bash
python -c "from tests.test_pipeline import test_salary_extraction; print(test_salary_extraction())"
```

Production runs happen via `.github/workflows/daily-job-search.yml` (cron `0 8 * * *` UTC, also `workflow_dispatch`). The workflow commits `data/seen.json` back to the repo, so the dedup store needs `contents: write` permission and no external storage.

## Architecture

This is a **one-user, one-profile** job-search pipeline — `config.yaml` is explicitly tuned to Valerian Demichev's CV (Senior/Lead Android, Kotlin+Compose, fintech). Scoring weights, excluded titles, and the $120k salary floor are not meant to be generic; treat them as personal configuration, not a reusable framework.

### Pipeline (see `src/main.py::pipeline`)

1. **Fan-out scraping** — `SCRAPERS` list in `main.py` calls each `scrapers/*.fetch(cfg)`. Each scraper catches its own errors but if one raises, the orchestrator logs and continues with the rest; a flaky source never fails the whole run.
2. **In-run dedup** by `Job.uniq_key()` (format: `"<source>:<id>"`) — the same listing often appears on multiple boards.
3. **Hard filters** (`matching.passes_filter`): title include/exclude, location must explicitly allow remote/EU/worldwide, salary floor. **Jobs with no disclosed salary pass the filter** but are penalized -2 in scoring — this is intentional (see Design notes below).
4. **Scoring** (`matching.score_job`) — additive keyword scoring over title+description against `stack_keywords` groups, plus salary tier bonuses and location bonuses. Returns human-readable `reasons` which get shown in the Telegram message.
5. **Cross-run dedup** via `SeenStore` (`src/state.py`, backed by `data/seen.json`). Entries older than `cfg["dedup_days"]` are evicted on save.
6. **Mark-after-ranking, save-before-sending**: the top N jobs are marked seen and the file is saved *before* the Telegram send. This means a Telegram failure will still "consume" those jobs — by design, to avoid hammering the chat with duplicates on retry. If you change this ordering, think about the retry story.
7. **Telegram packing** (`notifier.pack_into_messages`) — splits into ≤3800-char HTML messages (Telegram's hard limit is 4096). Uses `parse_mode=HTML`; escape user-provided strings with the local `_e()` helper, not raw string concatenation.

### Adding a scraper

Each scraper module exposes `fetch(cfg) -> Iterable[Job]` and uses `scrapers/base.http_get()` for retries + shared User-Agent. `Job` is the dataclass in `matching.py` — keep `source` unique per scraper and `id` stable across runs (this is the dedup key). If the source returns HTML, use `base.html_to_text()` so scoring sees clean text.

After adding the module, register it in `SCRAPERS` in `main.py`.

### Curated company list (`companies.yaml`)

The `greenhouse` and `lever` scrapers iterate `companies.yaml` and call each company's public board API. Both APIs are unauthenticated and have no rate-limiting issues at this volume (~40 companies, once a day). To add a company: find its careers URL — `boards.greenhouse.io/<slug>` → `greenhouse:` list; `jobs.lever.co/<slug>` → `lever:` list. To remove: delete the line. Slugs occasionally rename when companies migrate ATS; failures per-company are logged and the run continues.

### Design notes worth knowing

- **No-salary jobs pass filters but are penalized in score**: the user's explicit goal is a high-paying role, and many legit high-comp postings don't publish a range. Dropping them would be too aggressive; scoring them lower surfaces transparent listings first.
- **No LinkedIn/Indeed scraping** — both aggressively block, and the curated `companies.yaml` covers the relevant high-signal companies directly via Greenhouse/Lever public APIs. If you find a target company that isn't on Greenhouse or Lever (Ashby, Workable, SmartRecruiters), add a new ATS scraper rather than trying to scrape LinkedIn.
- **State is committed back to the repo** by the GitHub Action. This is load-bearing: it's how the job is stateful without external storage. If you change `data/seen.json`'s format, the workflow will still commit it, but old entries may become unreadable on next load (the store silently resets on parse errors — see `SeenStore._load`).
