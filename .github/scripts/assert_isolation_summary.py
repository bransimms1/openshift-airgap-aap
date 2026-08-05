#!/usr/bin/env python3
"""Assert a node-isolation summary matches the expected workflow exactly.

The summary reports only the nodes the harness ran, so a node quietly dropped
from its playbook list would otherwise leave CI green. The expected sequence is
declared here and every node must be present exactly once, with no extras.

  assert_isolation_summary.py <summary.json> passing
  assert_isolation_summary.py <summary.json> failing_dns
"""
from __future__ import annotations

import json
import sys

EXPECTED_NODES = [
    "00_bundle_intake",
    "01_validate_bundle",
    "10_validate_dns",
    "11_validate_ntp",
    "12_validate_registry",
    "13_validate_vips",
    "14_validate_bastion",
    "15_validate_bmc_redfish",
    "20_readiness_report",
]

# Artifacts that prove the set_stats handoff crossed a node boundary.
REQUIRED_ARTIFACTS = ["cluster_fqdn", "expected_dns", "readiness_report", "readiness_overall"]

SCENARIOS = {
    # scenario: (verdict, {node: status, ...} for nodes that must NOT be ok)
    "passing": ("READY_WITH_LIMITATIONS", {}),
    "failing_dns": ("NOT_READY", {"10_validate_dns": "failed", "20_readiness_report": "failed"}),
}


def check(summary: dict, scenario: str) -> list[str]:
    errs: list[str] = []
    want_verdict, want_failed = SCENARIOS[scenario]

    nodes = summary.get("nodes")
    if not isinstance(nodes, dict):
        return [f"summary has no usable 'nodes' mapping: {nodes!r}"]

    # Exact set: nothing missing, nothing extra. Duplicates cannot reach here -
    # the harness rejects a node reported twice - but the count is checked too.
    missing = [n for n in EXPECTED_NODES if n not in nodes]
    extra = [n for n in nodes if n not in EXPECTED_NODES]
    if missing:
        errs.append(f"missing node(s): {missing}")
    if extra:
        errs.append(f"unexpected node(s): {extra}")
    if len(nodes) != len(EXPECTED_NODES):
        errs.append(f"expected {len(EXPECTED_NODES)} nodes, summary has {len(nodes)}")

    if summary.get("scenario") != scenario:
        errs.append(f"summary is for scenario {summary.get('scenario')!r}, expected {scenario!r}")
    if summary.get("verdict") != want_verdict:
        errs.append(f"verdict is {summary.get('verdict')!r}, expected {want_verdict!r}")

    for node in EXPECTED_NODES:
        want = want_failed.get(node, "ok")
        got = nodes.get(node)
        if got != want:
            errs.append(f"{node} is {got!r}, expected {want!r}")

    artifacts = summary.get("artifacts") or []
    for artifact in REQUIRED_ARTIFACTS:
        if artifact not in artifacts:
            errs.append(f"artifact {artifact!r} did not cross node boundaries")

    return errs


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in SCENARIOS:
        print(f"usage: {sys.argv[0]} <summary.json> {'|'.join(SCENARIOS)}", file=sys.stderr)
        return 2
    try:
        with open(sys.argv[1], encoding="utf-8") as fh:
            summary = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"::error::could not read the isolation summary: {exc}")
        return 2

    errs = check(summary, sys.argv[2])
    for err in errs:
        print(f"::error::{sys.argv[2]}: {err}")
    if errs:
        return 1
    print(f"{sys.argv[2]}: all {len(EXPECTED_NODES)} nodes present with the expected status; "
          f"verdict {summary['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
