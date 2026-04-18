# Android Job Scout

A GitHub Actions + Python workflow that hunts remote Senior/Lead Android Engineer
roles every morning, scores them against Valerian Demichev's CV, fetches public
employee feedback from Reddit and HackerNews, and pushes the top matches to
Telegram.

## What it does

Every day at **08:00 UTC** (10:00/11:00 Europe/Sofia), the workflow:

1. Queries public job APIs and pages:
   - RemoteOK (JSON API)
   - Remotive (JSON API)
   - We Work Remotely (RSS)
   - Working Nomads (JSON API)
   - HackerNews "Ask HN: Who is hiring?" (current month)
   - Y Combinator "Work at a Startup" (HTML)
   - LinkedIn guest search + Indeed (best-effort, often rate-limited)
2. Hard-filters on:
   - Title contains `android` / `mobile` / `kotlin`, excludes junior/intern/React Native/iOS/etc.
   - Remote-friendly location (worldwide / Europe / EMEA) — no US-only roles
   - Disclosed salary ≥ **$120,000 USD** (jobs with no disclosed salary pass but are penalized in scoring)
3. Scores each match against CV-specific signals (Kotlin, Jetpack Compose,
   Clean Architecture, Dagger/Hilt, fintech, multi-module, CI/CD modernization…).
4. Dedups against `data/seen.json` so you don't get spammed with the same roles.
5. Fetches company-feedback snippets from Reddit and HackerNews — short quotes
   with links to the full threads, so you can sanity-check a company before
   applying.
6. Delivers the top 10 new matches to your Telegram chat as formatted messages.
7. Commits the updated seen-state back to the repo so dedup persists across runs.

## Setup

### 1. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, follow the prompts, pick a name and username.
3. Copy the **bot token** it gives you (format: `123456:ABC-DEF...`).
4. Open your bot's chat and send it any message (e.g. "hi") — this is required
   so it can message you back.
5. Get your **chat ID**: visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser. Look
   for `"chat":{"id":123456789,...}` in the JSON. That number is your chat ID.

### 2. Push this repo to GitHub

```bash
gh repo create android-job-scout --private --source=. --push
```

### 3. Add the secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**.

| Secret name           | Value                                       |
| --------------------- | ------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`  | The bot token from @BotFather               |
| `TELEGRAM_CHAT_ID`    | Your chat ID from the `getUpdates` response |

### 4. Enable workflow write permission

In your repo: **Settings → Actions → General → Workflow permissions**,
select **"Read and write permissions"** so the workflow can commit the
updated `data/seen.json`.

### 5. Trigger the first run

- Go to **Actions → Daily Android Job Scout → Run workflow** to run manually.
- Or wait for the next 08:00 UTC tick.

## Local testing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Dry-run: prints Telegram messages to stdout, doesn't send
python -m src.main --dry-run --no-state --top 5

# Send a real message to your Telegram chat
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python -m src.main --top 5
```

## Customization

Edit `config.yaml` to tune:

- `min_salary_usd` — the floor. Raise to 150k if you want to be stricter.
- `title_include_preferred` — titles that earn a scoring bonus.
- `title_exclude_any` — hard-filter titles you never want.
- `stack_keywords` — weights for matching your tech stack. The groups
  (`high_value`, `medium_value`, `domain_value`, `leadership_value`) map
  directly to scoring bonuses.
- `top_n` — how many matches to push per day.
- `dedup_days` — how long before a re-posted job can notify again.

## Design notes

- **Salary-aware**: jobs with disclosed salary ≥ $150k get +4, ≥ $180k get +6.
  Jobs with no disclosed salary are penalized (-2) since the user's explicit
  goal is a high-paying role that doesn't downgrade based on Bulgaria location.
- **Location-aware**: the filter requires explicit worldwide / Europe / EMEA
  allowance, and drops listings scoped to "US only" or "Canada only".
- **No paid APIs**: LinkedIn and Indeed are best-effort only since both
  aggressively block scraping. The remote-first boards (RemoteOK, Remotive,
  WWR, Working Nomads) are the main signal; HN "Who is hiring?" is
  surprisingly good for high-comp roles with transparent salaries.
- **Review signal**: Glassdoor is behind strong anti-bot walls, so we use
  Reddit + HN mentions instead. These are often higher-signal than
  Glassdoor anyway — first-hand engineering-culture commentary.
- **Dedup**: persistent `data/seen.json` committed back to the repo,
  so the workflow is stateful across runs without needing external storage.

## File layout

```
.
├── .github/workflows/daily-job-search.yml  # daily cron + manual dispatch
├── config.yaml                             # profile-tuned filter/scoring config
├── requirements.txt
├── src/
│   ├── main.py                             # orchestrator
│   ├── matching.py                         # scoring & filtering
│   ├── reviews.py                          # Reddit + HN company feedback
│   ├── notifier.py                         # Telegram sender
│   ├── state.py                            # seen-jobs dedup
│   └── scrapers/
│       ├── remoteok.py
│       ├── remotive.py
│       ├── weworkremotely.py
│       ├── workingnomads.py
│       ├── hackernews.py
│       ├── ycombinator.py
│       └── linkedin_indeed.py
└── data/
    └── seen.json                           # committed by the workflow
```
