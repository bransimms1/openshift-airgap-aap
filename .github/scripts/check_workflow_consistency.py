#!/usr/bin/env python3
"""Prove the isolation harness matches the real Controller workflow.

The harness's playbook list and assert_isolation_summary.py's EXPECTED_NODES are
hand-maintained. Edited together they stay consistent with each other while
drifting from the workflow the platform actually runs, so the Controller files
are the source of truth here.

Everything except the required-capability contract is DERIVED:

  * workflow nodes and their unified_job_template   <- workflow_templates.yml
  * job-template name -> playbook                   <- job_templates.yml
  * pre-approval vs post-approval                   <- the node parent graph

The only fixed list is REQUIRED_CAPABILITIES: the readiness checks this project
promises. A coordinated edit to every file cannot remove one of those without
failing here.

Parsing is structural, via PyYAML, which ansible-core already depends on.
A regex reader was formatting-sensitive: it saw only double-quoted scalars, so
a node added as 'unified_job_template: {single quotes}' was invisible to it.

  python3 .github/scripts/check_workflow_consistency.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - ansible-core installs PyYAML
    print("::error::PyYAML is required to parse the Controller definitions")
    sys.exit(2)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import assert_isolation_summary as summary  # noqa: E402

# The readiness capabilities this project promises. Removing one must fail CI
# however many other files agree, which is the whole point of pinning them.
REQUIRED_CAPABILITIES = [
    "00-bundle-intake",
    "01-validate-bundle",
    "10-validate-dns",
    "11-validate-ntp",
    "12-validate-registry",
    "13-validate-vips",
    "14-validate-bastion",
    "15-validate-bmc",
    "20-readiness-report",
]

CONTROLLER_MODULES = ("ansible.controller.", "awx.awx.")


def load(path: Path):
    """Structural load. Anchors, aliases and merge keys are resolved by PyYAML."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"::error::cannot parse {path.name}: {exc}")


def tasks_of(doc, path: Path) -> list[dict]:
    if not isinstance(doc, list) or not doc or not isinstance(doc[0], dict):
        raise SystemExit(f"::error::{path.name} is not a playbook with at least one play")
    tasks = doc[0].get("tasks")
    if not isinstance(tasks, list):
        raise SystemExit(f"::error::{path.name} play has no tasks list")
    return [t for t in tasks if isinstance(t, dict)]


def module_args(task: dict, suffix: str) -> dict | None:
    for key, value in task.items():
        if any(key.startswith(p) for p in CONTROLLER_MODULES) and key.endswith(suffix):
            if not isinstance(value, dict):
                raise SystemExit(f"::error::{key} arguments are not a mapping")
            return value
    return None


def workflow_nodes(path: Path) -> dict[str, dict]:
    """identifier -> {template, parents, is_approval}."""
    nodes: dict[str, dict] = {}
    for task in tasks_of(load(path), path):
        args = module_args(task, "workflow_job_template_node")
        if args is None:
            continue
        ident = args.get("identifier")
        template = args.get("unified_job_template")
        parents = [p.get("identifier") for p in args.get("parents") or [] if isinstance(p, dict)]
        loop = task.get("loop")

        if isinstance(loop, list):
            # One node definition expanded over a list of template names.
            if template != "{{ item }}" or ident != "{{ item }}":
                raise SystemExit(
                    f"::error::looped workflow node uses an unrecognised shape: "
                    f"identifier={ident!r} unified_job_template={template!r}"
                )
            for name in loop:
                if not isinstance(name, str):
                    raise SystemExit(f"::error::looped workflow node has a non-string entry: {name!r}")
                nodes[name] = {"template": name, "parents": parents, "is_approval": False}
            continue

        if ident is None:
            raise SystemExit("::error::a workflow node has no identifier")
        if template is None and "approval_node" not in args:
            raise SystemExit(f"::error::workflow node {ident!r} has neither a template nor an approval_node")
        nodes[ident] = {
            "template": template,
            "parents": parents,
            "is_approval": "approval_node" in args,
        }
    if not nodes:
        raise SystemExit("::error::no workflow nodes found")
    return nodes


def job_template_playbooks(path: Path) -> dict[str, str]:
    """job-template name -> playbook basename, derived from the definitions."""
    found: dict[str, str] = {}
    for task in tasks_of(load(path), path):
        args = module_args(task, "job_template")
        if args is None:
            continue
        loop = task.get("loop")
        if isinstance(loop, list):
            for entry in loop:
                if not isinstance(entry, dict) or "name" not in entry or "playbook" not in entry:
                    raise SystemExit(f"::error::job-template loop entry is not name+playbook: {entry!r}")
                found[entry["name"]] = Path(entry["playbook"]).stem
        else:
            name, playbook = args.get("name"), args.get("playbook")
            if isinstance(name, str) and isinstance(playbook, str) and "{{" not in name:
                found[name] = Path(playbook).stem
    if not found:
        raise SystemExit("::error::no job templates found")
    return found


def harness_playbooks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    start = text.find("PLAYBOOKS=(")
    if start == -1:
        raise SystemExit("::error::the harness has no PLAYBOOKS array")
    end = text.find(")", start)
    body = text[start + len("PLAYBOOKS=("):end]
    return [line.strip() for line in body.splitlines() if line.strip()]


def pre_approval(nodes: dict[str, dict]) -> set[str]:
    """Nodes that are not the approval node and are not downstream of it."""
    approvals = {i for i, n in nodes.items() if n["is_approval"]}
    downstream, changed = set(approvals), True
    while changed:
        changed = False
        for ident, node in nodes.items():
            if ident not in downstream and any(p in downstream for p in node["parents"]):
                downstream.add(ident)
                changed = True
    return {i for i in nodes if i not in downstream}


def check() -> list[str]:
    errs: list[str] = []
    nodes = workflow_nodes(ROOT / "controller" / "workflow_templates.yml")
    templates = job_template_playbooks(ROOT / "controller" / "job_templates.yml")

    readiness = sorted(
        nodes[i]["template"] for i in pre_approval(nodes) if nodes[i]["template"]
    )

    # 1. Required capabilities must all be present, whatever else changed.
    for capability in REQUIRED_CAPABILITIES:
        if capability not in readiness:
            errs.append(f"required readiness capability {capability!r} is not in the workflow")
    for extra in readiness:
        if extra not in REQUIRED_CAPABILITIES:
            errs.append(f"workflow runs pre-approval node {extra!r}, which is not a known capability")

    # 2. Every readiness node must have a job template with a playbook.
    expected: list[str] = []
    for name in readiness:
        if name not in templates:
            errs.append(f"workflow node {name!r} has no job template in job_templates.yml")
        else:
            expected.append(templates[name])
    expected.sort()

    # 3. The harness must run exactly those playbooks.
    harness = harness_playbooks(ROOT / "demo" / "node-isolation-test.sh")
    for missing in sorted(set(expected) - set(harness)):
        errs.append(f"harness is missing workflow playbook {missing!r}")
    for extra in sorted(set(harness) - set(expected)):
        errs.append(f"harness runs {extra!r}, which no pre-approval workflow node uses")

    # 4. The CI assertion must expect exactly those nodes.
    for missing in sorted(set(expected) - set(summary.EXPECTED_NODES)):
        errs.append(f"assert_isolation_summary is missing node {missing!r}")
    for extra in sorted(set(summary.EXPECTED_NODES) - set(expected)):
        errs.append(f"assert_isolation_summary expects {extra!r}, which the workflow does not run")

    return errs


def main() -> int:
    errs = check()
    for err in errs:
        print(f"::error::{err}")
    if errs:
        return 1
    print(f"workflow, job templates, harness and CI assertion agree on "
          f"{len(REQUIRED_CAPABILITIES)} readiness capabilities")
    return 0


if __name__ == "__main__":
    sys.exit(main())
