"""
llm_validator.py — remote-location eligibility check via an LLM.

After scoring and seen-dedup, we walk the ranked list top-down and ask an
open-weights LLM (hosted on Groq / OpenRouter / any OpenAI-compatible endpoint)
whether each posting is genuinely open to a remote candidate based in Europe
(specifically Bulgaria) or Ukraine. Jobs the model can't prove eligible from
the posting text are skipped; the next candidate is tried.

Verdicts are cached in data/llm_verdicts.json so we don't re-validate the
same job on every cron run. Cache shape mirrors SeenStore: one JSON file,
keyed by `Job.uniq_key()`, evicted after `dedup_days` on save.

Fallback policies when the API is unreachable, times out, or returns an
error (`on_unavailable` in config):
  - "skip"          — treat every job as not-proven, drop it.
  - "pass_through"  — skip validation entirely, keep the ranked list as-is.
                      Default; consistent with "one flaky source never fails
                      the whole run" from CLAUDE.md.
  - "fail"          — raise, aborting the run (useful when debugging).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from .matching import Job

log = logging.getLogger(__name__)


# ----------------------------- Verdict + cache ------------------------------

@dataclass
class LLMVerdict:
    eligible: bool
    confidence: float
    reason: str
    evidence_quote: str
    model: str
    validated_at: str  # ISO timestamp

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Optional["LLMVerdict"]:
        try:
            return cls(
                eligible=bool(d["eligible"]),
                confidence=float(d["confidence"]),
                reason=str(d.get("reason", "")),
                evidence_quote=str(d.get("evidence_quote", "")),
                model=str(d.get("model", "")),
                validated_at=str(d["validated_at"]),
            )
        except Exception:
            return None


class LLMVerdictCache:
    """Persistent cache of LLM verdicts. Same on-disk discipline as SeenStore."""

    def __init__(self, path: str, dedup_days: int):
        self.path = path
        self.dedup_days = dedup_days
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._data = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
            self._data = raw
        except Exception as e:
            log.warning("llm cache: failed to load %s: %s (starting fresh)", self.path, e)
            self._data = {}

    def get(self, key: str) -> Optional[LLMVerdict]:
        d = self._data.get(key)
        if not d:
            return None
        return LLMVerdict.from_dict(d)

    def put(self, key: str, verdict: LLMVerdict) -> None:
        self._data[key] = verdict.to_dict()

    def save(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.dedup_days)
        fresh: dict[str, dict] = {}
        for k, v in self._data.items():
            ts_str = v.get("validated_at") if isinstance(v, dict) else None
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except Exception:
                continue
            if ts >= cutoff:
                fresh[k] = v
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(fresh, f, indent=2, sort_keys=True)
        self._data = fresh
        log.info("llm cache: saved %d verdicts to %s", len(fresh), self.path)


# ----------------------------- LLM client -----------------------------------

class LLMUnavailable(Exception):
    """Raised when the LLM endpoint is unreachable, times out, or errors."""


# Description budget: longer postings add tokens without improving signal
# (the location/auth boilerplate always sits near the top or bottom).
# 1200 chars keeps each call comfortably under the free-tier TPM bucket.
_MAX_DESCRIPTION_CHARS = 1200


def build_prompt(job: Job, target_regions: list[str]) -> tuple[str, str]:
    """Return (system, user) messages for the chat endpoint."""
    regions = ", ".join(target_regions) if target_regions else "Europe, Bulgaria, Ukraine"
    system = (
        "You classify whether a job posting is open to a fully-remote candidate "
        f"based in: {regions}. "
        "Use ONLY the text provided; do not use outside knowledge. "
        "Default: eligible=false. "
        "Set eligible=true ONLY if the text explicitly allows hiring in one of "
        "the target regions (e.g. 'worldwide', 'anywhere', 'global', 'Europe', "
        "'EU', 'EEA', 'EMEA', Ukraine, or a specific European country). "
        "Set eligible=false if it restricts to US / Canada / LATAM / APAC, "
        "requires US/Canadian work authorization, names a non-target timezone, "
        "or is silent on geography. "
        "Respond as strict JSON only, keys exactly: "
        '{"eligible": bool, "confidence": 0-1 number, '
        '"reason": short string, "evidence_quote": verbatim short quote or ""}.'
    )
    desc = (job.description or "")[:_MAX_DESCRIPTION_CHARS]
    user = (
        f"Location: {job.location}\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"---\n{desc}"
    )
    return system, user


def _parse_response(content: str, model: str) -> LLMVerdict:
    """
    Parse the model's JSON reply. Fail closed: anything unparseable or missing
    the required keys becomes a not-eligible verdict at zero confidence.
    """
    data: Optional[dict] = None
    try:
        data = json.loads(content)
    except Exception:
        # Some models wrap JSON in prose or code fences. Grab the first {...}.
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None

    now = datetime.now(timezone.utc).isoformat()
    if not isinstance(data, dict) or "eligible" not in data:
        return LLMVerdict(
            eligible=False, confidence=0.0,
            reason="unparseable LLM response",
            evidence_quote="",
            model=model, validated_at=now,
        )

    try:
        confidence = float(data.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return LLMVerdict(
        eligible=bool(data.get("eligible")),
        confidence=confidence,
        reason=str(data.get("reason", ""))[:300],
        evidence_quote=str(data.get("evidence_quote", ""))[:300],
        model=model,
        validated_at=now,
    )


def call_llm(job: Job, cfg_llm: dict) -> LLMVerdict:
    """
    Call the OpenAI-compatible chat endpoint. Raises LLMUnavailable on any
    transport or API error so the caller can apply its fallback policy.
    """
    endpoint = cfg_llm["endpoint"]
    model = cfg_llm["model"]
    timeout = cfg_llm.get("timeout_sec", 30)
    api_key_env = cfg_llm.get("api_key_env", "GROQ_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise LLMUnavailable(f"env var {api_key_env} not set")

    system, user = build_prompt(job, cfg_llm.get("target_regions", []))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # One retry on 429 honoring the Retry-After header (capped at 60s to
    # keep pipeline runtime bounded). Groq's free tier is tokens-per-minute,
    # so a short wait is usually enough to get back under the bucket.
    max_wait = int(cfg_llm.get("retry_after_cap_sec", 60))
    for attempt in (1, 2):
        try:
            r = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            raise LLMUnavailable(f"request failed: {e}") from e
        if r.status_code == 429 and attempt == 1:
            try:
                wait = float(r.headers.get("Retry-After", "2"))
            except ValueError:
                wait = 2.0
            wait = min(max(wait, 1.0), max_wait)
            log.info("llm: 429 rate-limited, sleeping %.1fs before retry", wait)
            time.sleep(wait)
            continue
        break

    if r.status_code != 200:
        raise LLMUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")

    try:
        body = r.json()
        content = body["choices"][0]["message"]["content"]
    except Exception as e:
        raise LLMUnavailable(f"bad response shape: {e}") from e

    return _parse_response(content, model)


# ----------------------------- Ranked-walk validator ------------------------

def validate_ranked(
    scored: list[dict],
    cfg: dict,
    *,
    cache: Optional[LLMVerdictCache],
    need: int,
) -> list[dict]:
    """
    Walk `scored` top-down; for each item consult cache, then the LLM.
    Keep items whose verdict is eligible at >= min_confidence. Stop once
    `need` items have been kept or `max_candidates_to_validate` have been
    evaluated.

    Each kept item gains an "llm_verdict" key so the notifier can surface it.
    Returns the list of kept items in their original (score) order.
    """
    cfg_llm = cfg.get("llm_validation", {}) or {}
    if not cfg_llm.get("enabled"):
        return scored

    min_confidence = float(cfg_llm.get("min_confidence", 0.6))
    max_attempts = int(cfg_llm.get("max_candidates_to_validate", 40))
    on_unavailable = cfg_llm.get("on_unavailable", "pass_through")

    kept: list[dict] = []
    attempts = 0
    for item in scored:
        if len(kept) >= need:
            break
        if attempts >= max_attempts:
            log.info("llm: hit max_candidates_to_validate=%d, stopping", max_attempts)
            break
        attempts += 1

        job: Job = item["job"]
        key = job.uniq_key()

        verdict: Optional[LLMVerdict] = cache.get(key) if cache is not None else None
        if verdict is None:
            try:
                verdict = call_llm(job, cfg_llm)
            except LLMUnavailable as e:
                log.warning("llm: unavailable for %s: %s (policy=%s)", key, e, on_unavailable)
                if on_unavailable == "fail":
                    raise
                if on_unavailable == "pass_through":
                    # Stop making more calls but keep what we've already
                    # validated. Never re-emit jobs we already proved
                    # ineligible — that was the old bug.
                    log.warning("llm: bailing out with %d validated jobs so far", len(kept))
                    return kept
                # "skip": treat this job as not-proven and move on.
                continue
            if cache is not None:
                cache.put(key, verdict)

        if verdict.eligible and verdict.confidence >= min_confidence:
            item = dict(item)
            item["llm_verdict"] = verdict
            kept.append(item)
            log.info("llm: %s ELIGIBLE (%.2f) — %s", key, verdict.confidence, verdict.reason)
        else:
            log.info("llm: %s SKIP (eligible=%s conf=%.2f) — %s",
                     key, verdict.eligible, verdict.confidence, verdict.reason)

    log.info("llm: validated %d jobs in %d attempts", len(kept), attempts)
    return kept
