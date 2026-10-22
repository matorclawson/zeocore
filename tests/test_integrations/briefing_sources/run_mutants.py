"""Finite manual mutants; only behavioural assertion failures count as kills."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "src/zeo_core/integrations/briefing_sources"
TESTS = "tests/test_integrations/briefing_sources/"
MUTANTS = [
    (
        "M6 guard removed",
        "http.py",
        "host, path = self._guard(method, url, body)",
        'host, path = urlsplit(url).hostname or "", urlsplit(url).path',
        "test_http.py::test_guard_precedes_connection",
    ),
    (
        "M6 redirects followed",
        "http.py",
        "follow_redirects=False",
        "follow_redirects=True",
        "test_http.py::test_redirect_never_contacts_target",
    ),
    (
        "P cache removed",
        "http.py",
        "if cached and cached[0] > now:",
        "if False:",
        "test_http.py::test_cache_user_agent_and_no_personal_fields",
    ),
    (
        "P identifying UA removed",
        "http.py",
        'headers={"User-Agent": USER_AGENT}',
        "headers={}",
        "test_http.py::test_cache_user_agent_and_no_personal_fields",
    ),
    (
        "D provider object retained",
        "http.py",
        'if host == "api.adsb.lol" and path.startswith("/v2/"):\n',
        (
            'if host == "api.adsb.lol" and path.startswith("/v2/"):\n'
            "        return value\n"
        ),
        "test_http.py::test_cache_user_agent_and_no_personal_fields",
    ),
    (
        "K2 today invented",
        "market.py",
        '"quote_date": publication.isoformat()',
        '"quote_date": fetched_at.date().isoformat()',
        "test_market.py::test_daily_date_decimal_change_no_live_clock_or_unknown_row",
    ),
    (
        "K4 missing previous becomes zero",
        "market.py",
        "change = percent = None",
        "change = percent = 0.0",
        "test_market.py::test_missing_previous_value_means_change_unavailable_not_zero",
    ),
    (
        "K4 float arithmetic drift",
        "market.py",
        "delta / previous * Decimal(100)",
        "Decimal((float(current) - float(previous)) / float(previous) * 100)",
        "test_market.py::test_half_even_decimal_change_does_not_drift_through_float",
    ),
    (
        "M1 estimate ignores DataRef",
        "timeline.py",
        'estimate = flight.payload.get("estimated_arrival")',
        'estimate = "2026-10-22T00:00:00Z"',
        "test_timeline.py::test_timeline_margin_and_bound_assumption_not_model_prose",
    ),
    (
        "Cache custody mutable",
        "http.py",
        "return deepcopy(cached[1])",
        "return cached[1]",
        "test_http.py::test_cache_data_cannot_be_poisoned_by_a_caller",
    ),
]


def main() -> int:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    results: list[dict[str, str | int]] = []
    for index, (name, filename, old, new, selector) in enumerate(MUTANTS):
        path = SOURCE / filename
        original = path.read_text()
        if original.count(old) != 1:
            results.append({"name": name, "result": "INVALID target count"})
            continue
        try:
            path.write_text(original.replace(old, new))
            run = subprocess.run(  # noqa: S603 - fixed pytest argv and selectors
                [sys.executable, "-m", "pytest", TESTS + selector, "-q", "--no-cov"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            log = run.stdout + run.stderr
            (out / f"mutant-{index}.log").write_text(log)
            assertion = (
                "AssertionError" in log or "DID NOT RAISE" in log or "assert " in log
            )
            invalid = any(
                word in log
                for word in (
                    "ERROR collecting",
                    "SyntaxError",
                    "ImportError",
                    "NameError",
                    "ModuleNotFoundError",
                    "TypeError",
                )
            )
            killed = run.returncode == 1 and assertion and not invalid
            results.append(
                {
                    "name": name,
                    "result": "KILLED" if killed else "INVALID_OR_SURVIVED",
                    "exit": run.returncode,
                }
            )
        except subprocess.TimeoutExpired:
            results.append({"name": name, "result": "INVALID timeout"})
        finally:
            path.write_text(original)
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    return 0 if all(r["result"] == "KILLED" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
