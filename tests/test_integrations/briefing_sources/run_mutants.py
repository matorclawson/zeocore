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
        'headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}',
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

MUTANTS.extend(
    [
        (
            "A1 attribution withheld",
            "flight.py",
            "ODbL 1.0, Virtual",
            "ODbL 1.0; Virtual",
            "test_flight.py::test_all_adapter_attributions_survive_the_actual_shared_renderer",
        ),
        (
            "A2 CSV decoder escapes",
            "http.py",
            "            csv.Error,",
            "            ArithmeticError,",
            "test_http.py::test_decoder_limit_errors_are_value_free_source_unavailable",
        ),
        (
            "A2 recursive JSON escapes",
            "http.py",
            "            RecursionError,",
            "            ArithmeticError,",
            "test_http.py::test_decoder_limit_errors_are_value_free_source_unavailable",
        ),
        (
            "A3 stream deadline absent",
            "http.py",
            (
                "        for chunk in response.iter_bytes():\n"
                "            if self._monotonic() >= deadline:"
            ),
            "        for chunk in response.iter_bytes():\n            if False:",
            "test_http.py::test_dripping_stream_stops_at_monotonic_deadline_without_buffering_64k",
        ),
    ]
)

MUTANTS.append(
    (
        "F2 tiny change becomes zero",
        "market.py",
        "return float(value if rounded == 0 and value != 0 else rounded)",
        "return float(rounded)",
        "test_market.py::test_tiny_positive_and_negative_changes_never_become_invented_zero",
    )
)

MUTANTS.append(
    (
        "Market computed declaration missing",
        "market.py",
        "label=MARKET_CHANGE_ASSUMPTION",
        'label="undeclared_computation"',
        "test_market.py::test_daily_date_decimal_change_no_live_clock_or_unknown_row",
    )
)

MUTANTS.append(
    (
        "Rolling budget never expires",
        "http.py",
        "while window and window[0] <= now - 600:",
        "while False and window:",
        "test_http.py::test_twenty_five_spaced_briefings_keep_working_with_shared_budget",
    )
)
MUTANTS.append(
    (
        "Rolling budget unlimited",
        "http.py",
        "if len(window) >= 20:",
        "if False:",
        "test_http.py::test_rolling_window_burst_refused_then_recovers_without_a_new_client",
    )
)


MUTANTS.extend(
    [
        (
            "Routeset Created rejected",
            "http.py",
            "response.status_code == 201",
            "response.status_code == 299",
            "test_flight.py::test_routeset_created_status_keeps_valid_flight",
        ),
        (
            "Any 2xx accepted",
            "http.py",
            "response.status_code != 200 and not created_routeset",
            "not 200 <= response.status_code < 300 and not created_routeset",
            "test_http.py::test_created_is_only_allowed_for_routeset_post",
        ),
        (
            "Created GET accepted",
            "http.py",
            (
                '                    and method == "POST"\n'
                '                    and host == "api.adsb.lol"\n'
                '                    and path == "/api/0/routeset"'
            ),
            (
                '                    and method in ("POST", "GET")\n'
                '                    and host == "api.adsb.lol"'
            ),
            "test_http.py::test_created_is_only_allowed_for_routeset_post",
        ),
    ]
)


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
