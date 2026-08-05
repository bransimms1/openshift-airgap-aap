#!/usr/bin/env python3
"""Unit tests for assert_isolation_summary.py.

The point of that script is to fail when the workflow is incomplete, so these
tests are mostly mutations of a good summary.

  python3 .github/scripts/test_assert_isolation_summary.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assert_isolation_summary as a  # noqa: E402


def good(scenario: str) -> dict:
    nodes = {n: "ok" for n in a.EXPECTED_NODES}
    verdict = "READY_WITH_LIMITATIONS"
    if scenario == "failing_dns":
        nodes["10_validate_dns"] = "failed"
        nodes["20_readiness_report"] = "failed"
        verdict = "NOT_READY"
    return {
        "scenario": scenario,
        "verdict": verdict,
        "nodes": nodes,
        "failed_nodes": [k for k, v in nodes.items() if v == "failed"],
        "artifacts": list(a.REQUIRED_ARTIFACTS) + ["api_vips"],
    }


results: list[tuple[str, bool, str]] = []


def case(name: str, scenario: str, mutate, expect_error: bool) -> None:
    summary = copy.deepcopy(good(scenario))
    mutate(summary)
    errs = a.check(summary, scenario)
    ok = bool(errs) == expect_error
    detail = "caught" if expect_error else "quiet"
    results.append((name, ok, detail if ok else f"got {errs or 'nothing'}"))


case("clean passing summary is accepted", "passing", lambda s: None, False)
case("clean failing summary is accepted", "failing_dns", lambda s: None, False)

# The defect this script exists to catch: a node dropped from the harness.
case("a removed readiness node fails", "passing",
     lambda s: s["nodes"].pop("13_validate_vips"), True)
case("a removed intake node fails", "passing",
     lambda s: s["nodes"].pop("00_bundle_intake"), True)
case("a removed report node fails", "failing_dns",
     lambda s: s["nodes"].pop("20_readiness_report"), True)
case("an unexpected extra node fails", "passing",
     lambda s: s["nodes"].update({"99_surprise": "ok"}), True)
case("a node reported as failed in passing fails", "passing",
     lambda s: s["nodes"].update({"11_validate_ntp": "failed"}), True)
case("dns passing in failing_dns fails", "failing_dns",
     lambda s: s["nodes"].update({"10_validate_dns": "ok"}), True)
case("report passing in failing_dns fails", "failing_dns",
     lambda s: s["nodes"].update({"20_readiness_report": "ok"}), True)
case("an unrelated node failing in failing_dns fails", "failing_dns",
     lambda s: s["nodes"].update({"12_validate_registry": "failed"}), True)
case("wrong verdict fails", "passing",
     lambda s: s.update({"verdict": "READY"}), True)
case("wrong scenario fails", "passing",
     lambda s: s.update({"scenario": "failing_dns"}), True)
case("a missing artifact fails", "passing",
     lambda s: s.update({"artifacts": ["api_vips"]}), True)
case("an empty nodes mapping fails", "passing",
     lambda s: s.update({"nodes": {}}), True)
case("a non-mapping nodes value fails", "passing",
     lambda s: s.update({"nodes": "everything is fine"}), True)

width = max(len(n) for n, _, _ in results)
failed = 0
print("assert_isolation_summary unit tests\n")
for name, ok, detail in results:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed += 0 if ok else 1

print()
if failed:
    print(f"{failed} of {len(results)} tests failed.")
    sys.exit(1)
print(f"All {len(results)} tests passed.")
