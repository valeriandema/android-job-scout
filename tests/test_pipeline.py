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
from src.matching import Job, extract_salary_usd, is_us_only, passes_filter, score_job
from src.notifier import format_job, pack_into_messages
from src.state import SeenStore
from src import llm_validator
from src.llm_validator import (
    LLMUnavailable,
    LLMVerdict,
    LLMVerdictCache,
    _parse_response,
    build_prompt,
    validate_ranked,
)


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


def test_us_only_validation():
    """Sneaky US-only postings that currently pass the loose `remote` check."""
    cfg = load_cfg()

    sneaky_cases = [
        ("Remote (US only)", "Great Android role, Kotlin + Compose."),
        ("Remote", "Must be authorized to work in the United States."),
        ("Remote - USA", "We hire across all US timezones."),
        ("Anywhere", "This position is remote within the US."),
        ("Remote", "Candidates must currently reside in the US."),
        ("Remote", "US citizens only, W-2 employees."),
    ]
    not_us_only_cases = [
        ("Worldwide · Remote", "Globally distributed team."),
        ("Europe · Remote", "We're hiring in Europe and EMEA."),
        # Mentions US auth but overrides with worldwide hiring.
        ("Remote", "Globally distributed. US candidates must have work authorization in the US."),
    ]

    ok = True
    for location, desc in sneaky_cases:
        flagged = is_us_only(location, desc, cfg)
        passed, why = passes_filter(
            Job(source="t", id="x", title="Senior Android Engineer",
                company="Co", description=desc, location=location, url="x"),
            cfg,
        )
        line = f"  sneaky '{location} | {desc[:40]}': us_only={flagged} passes={passed} why={why}"
        print(line)
        ok = ok and flagged and not passed

    for location, desc in not_us_only_cases:
        flagged = is_us_only(location, desc, cfg)
        print(f"  legit '{location} | {desc[:40]}': us_only={flagged} (expected False)")
        ok = ok and not flagged

    return ok


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


def test_llm_prompt_shape():
    j = Job(source="t", id="1", title="Senior Android Engineer",
            company="Acme", description="Remote worldwide. Kotlin + Compose.",
            location="Worldwide", url="x")
    system, user = build_prompt(j, ["Europe", "Bulgaria", "Ukraine"])
    ok = True
    checks = [
        ("Bulgaria" in system, "system mentions Bulgaria"),
        ("Ukraine" in system, "system mentions Ukraine"),
        ("JSON" in system or "json" in system, "system demands JSON"),
        ("eligible" in system, "system specifies eligible key"),
        # New stricter rules — guard against regressing the prompt.
        ("Default: eligible=false" in system, "system defaults to deny"),
        ("EVEN IF every country on the list is European" in system,
         "system rejects enumerated-EU-only lists"),
        ("France, Germany, Italy, Spain, and Portugal" in system,
         "system carries negative example"),
        ("enumeration wins" in system, "system has conflict rule"),
        (j.title in user, "user carries title"),
        (j.description in user, "user carries description"),
    ]
    for cond, label in checks:
        print(f"  prompt check '{label}': {'OK' if cond else 'FAIL'}")
        ok = ok and cond
    return ok


def test_llm_verdict_parsing():
    cases = [
        # (raw content, expected_eligible, expected_confidence_range)
        ('{"eligible": true, "confidence": 0.9, "reason": "EU-friendly", "evidence_quote": "hiring in EMEA"}',
         True, (0.9, 0.9)),
        ('{"eligible": false, "confidence": 0.3, "reason": "silent on geography", "evidence_quote": ""}',
         False, (0.3, 0.3)),
        # Wrapped in prose -> regex fallback should still extract JSON
        ('Here is my verdict:\n{"eligible": true, "confidence": 0.7, "reason": "ok", "evidence_quote": "Europe"}\n',
         True, (0.7, 0.7)),
        # Garbage -> fail closed
        ('not even close to json', False, (0.0, 0.0)),
        # Missing required key -> fail closed
        ('{"confidence": 0.9}', False, (0.0, 0.0)),
        # Out-of-range confidence gets clamped
        ('{"eligible": true, "confidence": 5, "reason": "", "evidence_quote": ""}',
         True, (1.0, 1.0)),
    ]
    ok = True
    for raw, exp_elig, (lo, hi) in cases:
        v = _parse_response(raw, "test-model")
        elig_ok = v.eligible == exp_elig
        conf_ok = lo <= v.confidence <= hi
        label = raw[:40].replace("\n", " ")
        print(f"  parse '{label}': eligible={v.eligible} conf={v.confidence:.2f} "
              f"{'OK' if elig_ok and conf_ok else 'FAIL'}")
        ok = ok and elig_ok and conf_ok
    return ok


def test_llm_cache_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "llm_verdicts.json")
        c1 = LLMVerdictCache(path, dedup_days=30)
        assert c1.get("src:1") is None
        v = LLMVerdict(
            eligible=True, confidence=0.8,
            reason="global remote", evidence_quote="worldwide",
            model="test", validated_at="2026-04-19T00:00:00+00:00",
        )
        c1.put("src:1", v)
        c1.save()

        c2 = LLMVerdictCache(path, dedup_days=30)
        loaded = c2.get("src:1")
        assert loaded is not None
        assert loaded.eligible is True
        assert loaded.confidence == 0.8
        print("  llm cache round-trip: OK")

        # Entry older than dedup_days should evict on save.
        c2._data["src:1"]["validated_at"] = "2020-01-01T00:00:00+00:00"
        c2.save()
        c3 = LLMVerdictCache(path, dedup_days=30)
        assert c3.get("src:1") is None
        print("  llm cache TTL eviction: OK")
        return True


def _fake_llm(verdicts_by_key):
    """Build a fake call_llm that returns verdicts from a dict, or raises."""
    def _call(job, cfg_llm):
        v = verdicts_by_key.get(job.uniq_key())
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise AssertionError(f"unexpected call for {job.uniq_key()}")
        return v
    return _call


def test_validator_walks_ranked_list():
    cfg = {"llm_validation": {"enabled": True, "min_confidence": 0.6,
                              "max_candidates_to_validate": 10,
                              "on_unavailable": "skip"}}
    now = "2026-04-19T00:00:00+00:00"
    mk = lambda i: {"job": Job(source="t", id=str(i), title="Senior Android Engineer",
                               company="Co", description="", location="", url="x"),
                    "score": 10 - i, "reasons": []}
    scored = [mk(i) for i in range(5)]

    verdicts = {
        "t:0": LLMVerdict(False, 0.9, "US only", "US residents", "m", now),
        "t:1": LLMVerdict(True, 0.3, "weak signal", "", "m", now),   # below threshold
        "t:2": LLMVerdict(True, 0.8, "global", "worldwide", "m", now),
        "t:3": LLMVerdict(True, 0.75, "EU", "Europe", "m", now),
        "t:4": LLMVerdict(True, 0.9, "extra", "", "m", now),         # shouldn't be reached
    }
    original = llm_validator.call_llm
    try:
        llm_validator.call_llm = _fake_llm(verdicts)
        kept = validate_ranked(scored, cfg, cache=None, need=2)
    finally:
        llm_validator.call_llm = original

    ok = (len(kept) == 2
          and kept[0]["job"].id == "2"
          and kept[1]["job"].id == "3"
          and "llm_verdict" in kept[0])
    print(f"  ranked-walk kept={[k['job'].id for k in kept]} (expected ['2','3']): "
          f"{'OK' if ok else 'FAIL'}")
    return ok


def test_validator_fallback_policies():
    now = "2026-04-19T00:00:00+00:00"
    job = {"job": Job(source="t", id="1", title="Senior Android Engineer",
                      company="Co", description="", location="", url="x"),
           "score": 10, "reasons": []}
    job2 = {"job": Job(source="t", id="2", title="Senior Android Engineer",
                       company="Co", description="", location="", url="x"),
            "score": 9, "reasons": []}
    scored = [job, job2]

    original = llm_validator.call_llm
    ok = True
    try:
        # pass_through: first job validates, second job hits a transient API
        # error -> we bail but keep the already-validated first job. Never
        # re-emit un-validated jobs (that was the old bug).
        llm_validator.call_llm = _fake_llm({
            "t:1": LLMVerdict(True, 0.9, "global", "worldwide", "m", now),
            "t:2": LLMUnavailable("boom"),
        })
        cfg = {"llm_validation": {"enabled": True, "min_confidence": 0.6,
                                  "max_candidates_to_validate": 10,
                                  "on_unavailable": "pass_through"}}
        kept = validate_ranked(scored, cfg, cache=None, need=5)
        pt_ok = [k["job"].id for k in kept] == ["1"]
        print(f"  policy=pass_through kept={[k['job'].id for k in kept]} "
              f"(expected ['1']): {'OK' if pt_ok else 'FAIL'}")
        ok = ok and pt_ok

        # skip: API dies on t:1 but succeeds on t:2 with eligible verdict
        llm_validator.call_llm = _fake_llm({
            "t:1": LLMUnavailable("boom"),
            "t:2": LLMVerdict(True, 0.9, "global", "worldwide", "m", now),
        })
        cfg["llm_validation"]["on_unavailable"] = "skip"
        kept = validate_ranked(scored, cfg, cache=None, need=2)
        skip_ok = len(kept) == 1 and kept[0]["job"].id == "2"
        print(f"  policy=skip kept={[k['job'].id for k in kept]} (expected ['2']): "
              f"{'OK' if skip_ok else 'FAIL'}")
        ok = ok and skip_ok

        # fail: API dies -> exception propagates
        llm_validator.call_llm = _fake_llm({"t:1": LLMUnavailable("boom")})
        cfg["llm_validation"]["on_unavailable"] = "fail"
        raised = False
        try:
            validate_ranked(scored, cfg, cache=None, need=2)
        except LLMUnavailable:
            raised = True
        print(f"  policy=fail raised={raised}: {'OK' if raised else 'FAIL'}")
        ok = ok and raised
    finally:
        llm_validator.call_llm = original
    return ok


def main():
    results = {
        "salary extraction": test_salary_extraction(),
        "filter + score":    test_filtering_and_scoring(),
        "us-only guard":     test_us_only_validation(),
        "formatter":         test_formatter(),
        "seen store":        test_seen_store(),
        "llm prompt shape":  test_llm_prompt_shape(),
        "llm parsing":       test_llm_verdict_parsing(),
        "llm cache":         test_llm_cache_roundtrip(),
        "llm ranked walk":   test_validator_walks_ranked_list(),
        "llm fallback":      test_validator_fallback_policies(),
    }
    print()
    for k, v in results.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
