"""
matching.py — scoring and filtering logic tuned to Valerian Demichev's CV.

Given a Job dict with fields (title, company, description, location, salary_min,
salary_max, url, source), returns:
  - passes_filter(job, cfg) -> bool      (hard filters)
  - score_job(job, cfg) -> (score, reasons)

Salary is extracted heuristically from the description if not provided structurally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# ----------------------------- Job dataclass --------------------------------

@dataclass
class Job:
    source: str                          # "remoteok", "remotive", ...
    id: str                              # source-scoped unique id
    title: str
    company: str
    description: str
    location: str
    url: str
    salary_min: Optional[int] = None     # USD/year
    salary_max: Optional[int] = None     # USD/year
    tags: list[str] = field(default_factory=list)
    posted_at: Optional[str] = None      # ISO date if known

    def uniq_key(self) -> str:
        return f"{self.source}:{self.id}"


# ----------------------------- Salary parsing -------------------------------

# Matches things like:
#   "$120,000 - $160,000"
#   "USD 150k – 200k"
#   "€110k to €140k"
#   "150000 USD"
_SALARY_RE = re.compile(
    r"""
    (?:(?P<cur>\$|€|£|USD|EUR|GBP)\s*)?
    (?P<lo>\d{1,3}(?:[.,]\d{3})+|\d{2,7})\s*(?P<lo_k>k|K)?
    \s*(?:-|–|—|to)\s*
    (?:(?P<cur2>\$|€|£|USD|EUR|GBP)\s*)?
    (?P<hi>\d{1,3}(?:[.,]\d{3})+|\d{2,7})\s*(?P<hi_k>k|K)?
    """,
    re.VERBOSE,
)

# Crude currency normalization vs USD. Good enough for filtering.
_FX_TO_USD = {"$": 1.0, "USD": 1.0, "€": 1.08, "EUR": 1.08, "£": 1.27, "GBP": 1.27}


def _to_int(num_str: str, has_k: bool) -> Optional[int]:
    s = num_str.replace(",", "").replace(".", "")
    try:
        n = int(s)
    except ValueError:
        return None
    if has_k:
        n *= 1000
    # Sometimes a raw "150" is actually 150k.
    if n < 1000:
        n *= 1000
    return n


def extract_salary_usd(text: str) -> tuple[Optional[int], Optional[int]]:
    """Return (min_usd, max_usd) or (None, None)."""
    if not text:
        return None, None
    best = (None, None)
    best_mid = -1
    for m in _SALARY_RE.finditer(text):
        lo = _to_int(m.group("lo"), bool(m.group("lo_k")))
        hi = _to_int(m.group("hi"), bool(m.group("hi_k")))
        if lo is None or hi is None:
            continue
        # sanity: reject "3 - 5 years" style ranges
        if lo < 20000 or hi < 20000 or hi > 2_000_000:
            continue
        if hi < lo:
            lo, hi = hi, lo
        cur = (m.group("cur") or m.group("cur2") or "$").upper()
        fx = _FX_TO_USD.get(cur, 1.0)
        lo_usd = int(lo * fx)
        hi_usd = int(hi * fx)
        mid = (lo_usd + hi_usd) // 2
        if mid > best_mid:
            best_mid = mid
            best = (lo_usd, hi_usd)
    return best


# ----------------------------- Title filters --------------------------------

def _contains_any(text: str, needles: list[str]) -> bool:
    t = text.lower()
    return any(n.lower() in t for n in needles)


def passes_title_filter(title: str, cfg: dict) -> bool:
    t = title.lower()
    if _contains_any(t, cfg["title_exclude_any"]):
        return False
    if not _contains_any(t, cfg["title_include_any"]):
        return False
    return True


def passes_location_filter(location: str, description: str, cfg: dict) -> bool:
    """
    Location must either explicitly allow remote/EU/worldwide OR be empty/ambiguous
    and the description must mention remote. Hard-exclude US-only / on-site roles.
    """
    loc = (location or "").lower()
    desc = (description or "").lower()
    combined = f"{loc} {desc}"

    # Hard-exclude if location is ONLY US/Canada-only and nothing global.
    for bad in cfg["location_exclude_hard"]:
        if bad in combined and not any(g in combined for g in cfg["location_must_allow_any"]):
            return False

    # Must mention remote/worldwide/EU somewhere.
    return _contains_any(combined, cfg["location_must_allow_any"])


def passes_salary_filter(job: Job, cfg: dict) -> bool:
    min_floor = cfg["min_salary_usd"]
    # If we have disclosed salary, use the max (upper bound) to decide.
    if job.salary_max:
        return job.salary_max >= min_floor
    if job.salary_min:
        return job.salary_min >= min_floor
    # No salary disclosed — keep it, but score will be penalized.
    return True


# ----------------------------- Scoring --------------------------------------

def score_job(job: Job, cfg: dict) -> tuple[int, list[str]]:
    """Higher = better fit. Returns (score, human-readable reasons)."""
    score = 0
    reasons: list[str] = []

    title = job.title.lower()
    desc = (job.description or "").lower()
    haystack = f"{title} {desc}"

    # Preferred title seniority
    for kw in cfg["title_include_preferred"]:
        if kw in title:
            score += 4
            reasons.append(f"title has '{kw}' (+4)")

    # High-value stack hits
    for kw in cfg["stack_keywords"]["high_value"]:
        if kw in haystack:
            score += 3
            reasons.append(f"stack: {kw} (+3)")
    for kw in cfg["stack_keywords"]["medium_value"]:
        if kw in haystack:
            score += 2
            reasons.append(f"stack: {kw} (+2)")
    for kw in cfg["stack_keywords"]["domain_value"]:
        if kw in haystack:
            score += 2
            reasons.append(f"domain: {kw} (+2)")
    for kw in cfg["stack_keywords"]["leadership_value"]:
        if kw in haystack:
            score += 2
            reasons.append(f"leadership: {kw} (+2)")

    # Salary bonus — reward higher disclosed comp
    if job.salary_max and job.salary_max >= 180_000:
        score += 6
        reasons.append(f"salary max ${job.salary_max:,} (+6)")
    elif job.salary_max and job.salary_max >= 150_000:
        score += 4
        reasons.append(f"salary max ${job.salary_max:,} (+4)")
    elif job.salary_max and job.salary_max >= cfg["min_salary_usd"]:
        score += 2
        reasons.append(f"salary max ${job.salary_max:,} (+2)")
    elif not job.salary_min and not job.salary_max:
        score -= 2
        reasons.append("no salary disclosed (-2)")

    # Location bonus for EU/global friendliness
    loc = (job.location or "").lower()
    if any(k in loc for k in ("worldwide", "anywhere", "global")):
        score += 3
        reasons.append("truly global remote (+3)")
    elif any(k in loc for k in ("europe", "emea", "eu")):
        score += 2
        reasons.append("EU-friendly (+2)")

    # Anti-signal: US timezone restrictions
    if "us timezone" in haystack or "est timezone" in haystack or "pst timezone" in haystack:
        score -= 3
        reasons.append("US-timezone restriction (-3)")

    return score, reasons


def passes_filter(job: Job, cfg: dict) -> tuple[bool, str]:
    if not passes_title_filter(job.title, cfg):
        return False, "title filter"
    if not passes_location_filter(job.location, job.description, cfg):
        return False, "location filter"
    if not passes_salary_filter(job, cfg):
        return False, "salary filter"
    return True, ""
