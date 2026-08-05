#!/usr/bin/env python3
"""Prove the isolation harness matches the real Controller workflow.

The harness's playbook list and assert_isolation_summary.py's EXPECTED_NODES are
two hand-maintained lists. Edited together they stay consistent with each other
while drifting from the workflow the platform actually runs. The Controller
definition is the source of truth, so all three are compared here.

  python3 .github/scripts/check_workflow_consistency.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / "controller" / "workflow_templates.yml"
JOB_TEMPLATES = ROOT / "controller" / "job_templates.yml"
HARNESS = ROOT / "demo" / "node-isolation-test.sh"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assert_isolation_summary as summary  # noqa: E402

# Nodes the readiness workflow runs before the approval gate. Execution nodes
# (30/31/32) sit after it and are deliberately not part of the harness: demo
# mode refuses to act, so running them proves nothing about readiness.
POST_APPROVAL = {"30-bmc-prepare-hosts", "31-generate-agent-iso", "32-boot-virtual-media"}

# The workflow names templates with hyphens; playbooks and summary keys use
# underscores, and 15-validate-bmc runs 15_validate_bmc_redfish.yml.
TEMPLATE_TO_PLAYBOOK = {
    "00-bundle-intake": "00_bundle_intake",
    "01-validate-bundle": "01_validate_bundle",
    "10-validate-dns": "10_validate_dns",
    "11-validate-ntp": "11_validate_ntp",
    "12-validate-registry": "12_validate_registry",
    "13-validate-vips": "13_validate_vips",
    "14-validate-bastion": "14_validate_bastion",
    "15-validate-bmc": "15_validate_bmc_redfish",
    "20-readiness-report": "20_readiness_report",
}


def workflow_templates(text: str) -> set[str]:
    """Job-template names referenced by workflow nodes.

    Read with a narrow regex rather than a YAML parser: the file uses merge-key
    anchors that a plain safe_load rejects, and running ansible.controller here
    is out of the question.
    """
    named = set(re.findall(r'unified_job_template:\s*"([^"]+)"', text))
    # Nodes created in a loop carry "{{ item }}" and list the real names in the
    # loop body underneath.
    for block in re.findall(r'unified_job_template:\s*"\{\{ item \}\}".*?\n      loop:\n((?:\s+- "[^"]+"\n)+)', text, re.S):
        named.update(re.findall(r'- "([^"]+)"', block))
    named.discard("{{ item }}")
    return named


def job_template_playbooks(text: str) -> dict[str, str]:
    """name -> playbook, from every job-template definition."""
    pairs = re.findall(r'name:\s*"([0-9]{2}-[a-z-]+)",\s*playbook:\s*"playbooks/([a-z0-9_]+)\.yml"', text)
    pairs += re.findall(r'name:\s*"([0-9]{2}-[a-z-]+)"\n\s*playbook:\s*"playbooks/([a-z0-9_]+)\.yml"', text)
    return dict(pairs)


def harness_playbooks(text: str) -> list[str]:
    block = re.search(r"PLAYBOOKS=\(\n(.*?)\n\)", text, re.S)
    if not block:
        return []
    return [line.strip() for line in block.group(1).splitlines() if line.strip()]


def check() -> list[str]:
    errs: list[str] = []
    wf_text = WORKFLOW.read_text(encoding="utf-8")
    jt_text = JOB_TEMPLATES.read_text(encoding="utf-8")

    templates = workflow_templates(wf_text)
    readiness = {t for t in templates if t not in POST_APPROVAL}

    # 1. Every readiness template must have a known playbook mapping.
    unmapped = sorted(t for t in readiness if t not in TEMPLATE_TO_PLAYBOOK)
    if unmapped:
        errs.append(f"workflow node(s) with no known playbook mapping: {unmapped}")
    stale = sorted(t for t in TEMPLATE_TO_PLAYBOOK if t not in readiness)
    if stale:
        errs.append(f"mapping names template(s) the workflow does not run: {stale}")

    expected_from_workflow = sorted(
        TEMPLATE_TO_PLAYBOOK[t] for t in readiness if t in TEMPLATE_TO_PLAYBOOK
    )

    # 2. The harness must run exactly those playbooks.
    harness = harness_playbooks(HARNESS.read_text(encoding="utf-8"))
    if sorted(harness) != expected_from_workflow:
        missing = sorted(set(expected_from_workflow) - set(harness))
        extra = sorted(set(harness) - set(expected_from_workflow))
        if missing:
            errs.append(f"harness is missing workflow node(s): {missing}")
        if extra:
            errs.append(f"harness runs node(s) the workflow does not: {extra}")

    # 3. The CI assertion must expect exactly those nodes.
    if sorted(summary.EXPECTED_NODES) != expected_from_workflow:
        missing = sorted(set(expected_from_workflow) - set(summary.EXPECTED_NODES))
        extra = sorted(set(summary.EXPECTED_NODES) - set(expected_from_workflow))
        if missing:
            errs.append(f"assert_isolation_summary is missing node(s): {missing}")
        if extra:
            errs.append(f"assert_isolation_summary expects node(s) not in the workflow: {extra}")

    # 4. Each workflow node must point at the playbook the mapping claims.
    defined = job_template_playbooks(jt_text)
    for template, playbook in sorted(TEMPLATE_TO_PLAYBOOK.items()):
        if template not in defined:
            errs.append(f"job template {template!r} is not defined in controller/job_templates.yml")
        elif defined[template] != playbook:
            errs.append(
                f"job template {template!r} runs {defined[template]}.yml, "
                f"but the mapping expects {playbook}.yml"
            )

    return errs


def main() -> int:
    errs = check()
    for err in errs:
        print(f"::error::{err}")
    if errs:
        return 1
    print(f"harness, CI assertion and Controller workflow agree on "
          f"{len(TEMPLATE_TO_PLAYBOOK)} readiness nodes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
