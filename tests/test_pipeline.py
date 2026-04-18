"""
Offline sanity tests — exercise the pure logic (no network required).

Run with:  python -m tests.test_pipeline
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yaml
from src.matching import Job, extract_salary_usd, passes_filter, score_job
from src.notifier import format_job, pack_into_messages
from src.state import SeenStore


def load_cfg():
    with open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")) as f:
        return yaml.safe_load(f)


def test_salary_extraction():
    cases = [
        ("Salary: $140,000 - $180,000 USD", (140_000, 180_000)),
        ("We pay €110k – €140k depending on experience", (118_800, 151_200)),  # 1.08 fx
        ("$150k+", (None, None)),     # no range pattern
        ("Experience: 3-5 years", (None, None)),
        ("Compensation range: 200000 to 250000 USD", (200_000, 250_000)),
    ]
    ok = True
    for text, expected in cases:
        got = extract_salary_usd(text)
        match = got == expected
        print(f"  salary '{text[:50]}': got={got} expected={expected} {'OK' if match else 'FAIL'}")
        ok = ok and match
    return ok


def test_filtering_and_scoring():
    cfg = load_cfg()

    good = Job(
        source="test", id="g1",
        title="Senior Android Engineer — Kotlin / Compose",
        company="FintechCo",
        description=(
            "Build our mobile banking app in Kotlin with Jetpack Compose. "
            "You'll work on a multi-module Clean Architecture codebase with Dagger 2, "
            "Retrofit, Room, and Coroutines. Experience with fintech, KYC, biometric "
            "auth preferred. Mentoring juniors and code review expected. "
            "Salary: $150,000 - $200,000 USD. 100% remote, worldwide."
        ),
        location="Worldwide · Remote",
        url="https://example.com/job/1",
        salary_min=150_000, salary_max=200_000,
    )

    bad_junior = Job(
        source="test", id="b1",
        title="Junior Android Developer",
        company="Co", description="Entry level role.", location="Worldwide",
        url="x",
    )

    bad_us_only = Job(
        source="test", id="b2",
        title="Senior Android Engineer",
        company="Co",
        description="USA only, on-site in NYC.",
        location="United States only",
        url="x",
    )

    low_salary = Job(
        source="test", id="b3",
        title="Senior Android Engineer",
        company="Co", description="Remote worldwide.",
        location="Worldwide", url="x",
        salary_min=60_000, salary_max=80_000,
    )

    ok1, _ = passes_filter(good, cfg)
    ok2, why2 = passes_filter(bad_junior, cfg)
    ok3, why3 = passes_filter(bad_us_only, cfg)
    ok4, why4 = passes_filter(low_salary, cfg)

    print(f"  good job passes: {ok1} (expected True)")
    print(f"  junior rejected: {not ok2} reason={why2}")
    print(f"  US-only rejected: {not ok3} reason={why3}")
    print(f"  low-salary rejected: {not ok4} reason={why4}")

    s, reasons = score_job(good, cfg)
    print(f"  good job score: {s}  (first reasons: {reasons[:3]})")

    return ok1 and not ok2 and not ok3 and not ok4 and s > 20


def test_formatter():
    cfg = load_cfg()
    j = Job(
        source="remoteok", id="1",
        title="Senior Android Engineer",
        company="Acme Fintech",
        description="Kotlin, Jetpack Compose, Clean Architecture.",
        location="Europe · Remote",
        url="https://example.com/j/1",
        salary_min=150_000, salary_max=200_000,
    )
    s, reasons = score_job(j, cfg)
    block = format_job(j, s, reasons)
    print("  formatted block:")
    for line in block.splitlines():
        print(f"    {line}")
    msgs = pack_into_messages(
        header="🤖 <b>Test digest</b> — 2026-04-18",
        blocks=[block] * 3,
        footer="— end —",
    )
    print(f"  packed into {len(msgs)} message(s), lengths={[len(m) for m in msgs]}")
    return all(len(m) <= 4000 for m in msgs)


def test_seen_store():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "seen.json")
        s1 = SeenStore(path, dedup_days=30)
        assert not s1.has("remoteok:123")
        s1.mark("remoteok:123")
        s1.save()

        s2 = SeenStore(path, dedup_days=30)
        assert s2.has("remoteok:123")
        print("  seen store round-trip: OK")
        return True


def main():
    results = {
        "salary extraction": test_salary_extraction(),
        "filter + score":    test_filtering_and_scoring(),
        "formatter":         test_formatter(),
        "seen store":        test_seen_store(),
    }
    print()
    for k, v in results.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
