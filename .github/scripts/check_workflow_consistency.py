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
# The intended dependency stages. Membership alone would let the graph be
# rearranged into something complete but meaningless - every readiness check
# hanging directly off intake, say, or approval attached to a single branch.
ROOT_CAPABILITY = "00-bundle-intake"
BUNDLE_VALIDATION = "01-validate-bundle"
READINESS_BRANCHES = [
    "10-validate-dns",
    "11-validate-ntp",
    "12-validate-registry",
    "13-validate-vips",
    "14-validate-bastion",
    "15-validate-bmc",
]
REPORT_CAPABILITY = "20-readiness-report"
APPROVAL_IDENTIFIER = "approve-execute"
EXECUTION_TEMPLATES = ["30-bmc-prepare-hosts", "31-generate-agent-iso", "32-boot-virtual-media"]

# Edge types Automation Controller accepts on a workflow node parent.
SUPPORTED_EDGE_TYPES = ("success", "failure", "always")

# EDGE TYPES ARE THE SAFETY PROPERTY, not decoration.
#
#   branch --always--> report    the report runs on the failure path too, so a
#                                failed run still produces its evidence
#   report --success--> approval the report fails deliberately on NOT_READY, so
#                                the success edge is not taken and the gate is
#                                unreachable
#   approval --success--> 30 --success--> 31 --success--> 32
#
# Validating parent IDENTIFIERS alone let all three be changed while the graph
# still looked correct. Every parent in controller/workflow_templates.yml
# declares an explicit type today, so a missing type is treated as an error
# rather than defaulted to anything.
BRANCH_TO_REPORT_EDGE = "always"
REPORT_TO_APPROVAL_EDGE = "success"
EXECUTION_EDGE = "success"

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
    """Tasks from EVERY play. Reading only doc[0] meant a second play - which
    Automation Controller executes - was invisible to this check."""
    if not isinstance(doc, list) or not doc:
        raise SystemExit(f"::error::{path.name} is not a playbook with at least one play")
    tasks: list[dict] = []
    for index, play in enumerate(doc):
        if not isinstance(play, dict):
            raise SystemExit(f"::error::{path.name} play {index} is not a mapping")
        play_tasks = play.get("tasks")
        if play_tasks is None:
            continue
        if not isinstance(play_tasks, list):
            raise SystemExit(f"::error::{path.name} play {index} has a non-list tasks value")
        tasks.extend(t for t in play_tasks if isinstance(t, dict))
    if not tasks:
        raise SystemExit(f"::error::{path.name} defines no tasks")
    return tasks


def module_args(task: dict, suffix: str) -> dict | None:
    for key, value in task.items():
        if any(key.startswith(p) for p in CONTROLLER_MODULES) and key.endswith(suffix):
            if not isinstance(value, dict):
                raise SystemExit(f"::error::{key} arguments are not a mapping")
            return value
    return None


def parse_parents(args: dict, owner: str) -> list[dict]:
    """Parent relationships, each keeping its identifier AND its edge type.

    Reducing these to identifier strings discarded the edge types, which are the
    only thing distinguishing a workflow that gates execution from one that does
    not.
    """
    raw = args.get("parents") or []
    if not isinstance(raw, list):
        raise SystemExit(f"::error::node {owner!r} has a non-list parents value")
    parents: list[dict] = []
    seen: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise SystemExit(f"::error::node {owner!r} has a parent that is not a mapping: {entry!r}")
        identifier = entry.get("identifier")
        if not isinstance(identifier, str) or not identifier.strip():
            raise SystemExit(f"::error::node {owner!r} has a parent with no identifier: {entry!r}")
        edge = entry.get("type")
        if edge is None:
            raise SystemExit(
                f"::error::node {owner!r} parent {identifier!r} has no edge type; "
                f"declare one of {'/'.join(SUPPORTED_EDGE_TYPES)} explicitly"
            )
        if edge not in SUPPORTED_EDGE_TYPES:
            raise SystemExit(
                f"::error::node {owner!r} parent {identifier!r} has unsupported edge "
                f"type {edge!r}; expected one of {'/'.join(SUPPORTED_EDGE_TYPES)}"
            )
        if identifier in seen and seen[identifier] != edge:
            raise SystemExit(
                f"::error::node {owner!r} declares parent {identifier!r} twice with "
                f"conflicting edge types {seen[identifier]!r} and {edge!r}"
            )
        seen[identifier] = edge
        parents.append({"identifier": identifier, "type": edge})
    return parents


def parent_ids(node: dict) -> list[str]:
    return [p["identifier"] for p in node["parents"]]


def edge_type(nodes: dict[str, dict], child: str, parent: str) -> str | None:
    """The edge type from `parent` into `child`, or None if there is no edge."""
    if child not in nodes:
        return None
    for entry in nodes[child]["parents"]:
        if entry["identifier"] == parent:
            return entry["type"]
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
        parents = parse_parents(args, ident if isinstance(ident, str) else "<unnamed>")
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
                if name in nodes:
                    raise SystemExit(f"::error::duplicate workflow node identifier {name!r}")
                nodes[name] = {"template": name, "parents": parents, "is_approval": False}
            continue

        if ident is None:
            raise SystemExit("::error::a workflow node has no identifier")
        if template is None and "approval_node" not in args:
            raise SystemExit(f"::error::workflow node {ident!r} has neither a template nor an approval_node")
        if ident in nodes:
            raise SystemExit(f"::error::duplicate workflow node identifier {ident!r}")
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
                if entry["name"] in found:
                    raise SystemExit(f"::error::duplicate job template name {entry['name']!r}")
                found[entry["name"]] = Path(entry["playbook"]).stem
        else:
            name, playbook = args.get("name"), args.get("playbook")
            if isinstance(name, str) and isinstance(playbook, str) and "{{" not in name:
                if name in found:
                    raise SystemExit(f"::error::duplicate job template name {name!r}")
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


def validate_graph(nodes: dict[str, dict]) -> list[str]:
    """Structural checks on the workflow graph itself."""
    errs: list[str] = []

    # Parents must exist. An unresolvable reference is a broken definition, not
    # merely an unreachable node.
    for ident, node in nodes.items():
        for parent in parent_ids(node):
            if parent not in nodes:
                errs.append(f"node {ident!r} names a parent {parent!r} that does not exist")

    # Cycles. Nothing in a cycle can ever run.
    colour: dict[str, int] = {}

    def visit(ident: str, trail: list[str]) -> None:
        if colour.get(ident) == 2:
            return
        if colour.get(ident) == 1:
            cycle = trail[trail.index(ident):] + [ident]
            errs.append(f"workflow graph has a cycle: {' -> '.join(cycle)}")
            return
        colour[ident] = 1
        for parent in parent_ids(nodes[ident]):
            if parent in nodes:
                visit(parent, trail + [ident])
        colour[ident] = 2

    for ident in nodes:
        visit(ident, [])
    if any("cycle" in e for e in errs):
        return errs  # reachability is meaningless once the graph is cyclic

    # Exactly one approval node, with the expected identifier.
    approvals = sorted(i for i, n in nodes.items() if n["is_approval"])
    if len(approvals) != 1:
        errs.append(f"expected exactly one approval node, found {approvals or 'none'}")
    elif approvals[0] != APPROVAL_IDENTIFIER:
        errs.append(f"approval node is {approvals[0]!r}, expected {APPROVAL_IDENTIFIER!r}")

    return errs


def ancestors(nodes: dict[str, dict], ident: str) -> set[str]:
    """Every node upstream of `ident`."""
    seen: set[str] = set()
    stack = parent_ids(nodes[ident]) if ident in nodes else []
    while stack:
        current = stack.pop()
        if current in seen or current not in nodes:
            continue
        seen.add(current)
        stack.extend(parent_ids(nodes[current]))
    return seen


def validate_stages(nodes: dict[str, dict], pre: set[str]) -> list[str]:
    """Prove the intended dependency stages, without imposing an ordering
    among the parallel readiness branches."""
    errs: list[str] = []
    by_template = {n["template"]: i for i, n in nodes.items() if n["template"]}

    def node_for(template: str) -> str | None:
        return by_template.get(template)

    root = node_for(ROOT_CAPABILITY)
    if root is None:
        return [f"{ROOT_CAPABILITY!r} is not in the workflow"]

    # Exactly one readiness root, and it is intake.
    roots = sorted(i for i in pre if not nodes[i]["parents"])
    if roots != [root]:
        errs.append(
            f"expected {root!r} to be the only readiness node without parents, found {roots}"
        )

    # Every readiness node reachable from intake.
    for ident in sorted(pre):
        if ident != root and root not in ancestors(nodes, ident):
            errs.append(f"readiness node {ident!r} is not reachable from {ROOT_CAPABILITY!r}")

    validation = node_for(BUNDLE_VALIDATION)
    report = node_for(REPORT_CAPABILITY)
    if validation is None or report is None:
        return errs + [f"{BUNDLE_VALIDATION!r} or {REPORT_CAPABILITY!r} is not in the workflow"]

    if root not in ancestors(nodes, validation):
        errs.append(f"{BUNDLE_VALIDATION!r} does not depend on {ROOT_CAPABILITY!r}")

    for template in READINESS_BRANCHES:
        branch = node_for(template)
        if branch is None:
            errs.append(f"readiness branch {template!r} is not in the workflow")
            continue
        if validation not in ancestors(nodes, branch):
            errs.append(f"readiness branch {template!r} does not depend on {BUNDLE_VALIDATION!r}")
        if branch not in ancestors(nodes, report):
            errs.append(f"{REPORT_CAPABILITY!r} does not depend on branch {template!r}")

    if APPROVAL_IDENTIFIER in nodes:
        approval_parents = ancestors(nodes, APPROVAL_IDENTIFIER)
        if report not in approval_parents:
            errs.append(f"the approval node does not depend on {REPORT_CAPABILITY!r}")
        # No readiness check may sit BEHIND the gate. A branch moved downstream
        # of approval would still be a member of the workflow while no longer
        # contributing to the evidence the approver sees.
        for ident in sorted(pre):
            if APPROVAL_IDENTIFIER in ancestors(nodes, ident):
                errs.append(f"readiness node {ident!r} is downstream of the approval node")

        # Execution must be downstream of approval, with no bypass path.
        for template in EXECUTION_TEMPLATES:
            execution = node_for(template)
            if execution is None:
                errs.append(f"execution node {template!r} is not in the workflow")
            elif APPROVAL_IDENTIFIER not in ancestors(nodes, execution):
                errs.append(f"execution node {template!r} is not downstream of the approval node")

    return errs


def validate_edge_types(nodes: dict[str, dict]) -> list[str]:
    """Prove the edge types the safety behaviour depends on.

    Identifier-only validation could not tell these apart:
      * a report whose branch edges are `success` never runs on the failure
        path, so a failed run produces no evidence;
      * an approval node reached by an `always` edge is reachable even when the
        report fails on NOT_READY, which removes the gate entirely.
    """
    errs: list[str] = []
    by_template = {n["template"]: i for i, n in nodes.items() if n["template"]}

    report = by_template.get(REPORT_CAPABILITY)
    if report is None:
        return [f"{REPORT_CAPABILITY!r} is not in the workflow"]

    # 1. Every readiness branch feeds the report through an `always` edge.
    for template in READINESS_BRANCHES:
        branch = by_template.get(template)
        if branch is None:
            errs.append(f"readiness branch {template!r} is not in the workflow")
            continue
        edge = edge_type(nodes, report, branch)
        if edge is None:
            errs.append(f"{template!r} is not a parent of {REPORT_CAPABILITY!r}")
        elif edge != BRANCH_TO_REPORT_EDGE:
            errs.append(
                f"{template!r} feeds {REPORT_CAPABILITY!r} through a {edge!r} edge, "
                f"expected {BRANCH_TO_REPORT_EDGE!r}; the report must run on the "
                f"failure path so a failed run still produces evidence"
            )

    # 2. The report feeds approval through a `success` edge, and nothing else
    #    reaches approval directly.
    if APPROVAL_IDENTIFIER not in nodes:
        errs.append(f"approval node {APPROVAL_IDENTIFIER!r} is not in the workflow")
        return errs

    edge = edge_type(nodes, APPROVAL_IDENTIFIER, report)
    if edge is None:
        errs.append(f"{REPORT_CAPABILITY!r} is not a direct parent of the approval node")
    elif edge != REPORT_TO_APPROVAL_EDGE:
        errs.append(
            f"{REPORT_CAPABILITY!r} feeds the approval node through a {edge!r} edge, "
            f"expected {REPORT_TO_APPROVAL_EDGE!r}; the report fails deliberately on "
            f"NOT_READY, and only a success edge makes the gate unreachable"
        )

    for entry in nodes[APPROVAL_IDENTIFIER]["parents"]:
        if entry["identifier"] == report:
            continue
        if nodes.get(entry["identifier"], {}).get("template") in READINESS_BRANCHES:
            errs.append(
                f"the approval node is reachable directly from readiness branch "
                f"{entry['identifier']!r}; it must depend only on {REPORT_CAPABILITY!r}"
            )

    # 3. The execution chain runs on success edges, in order, behind approval.
    chain = [APPROVAL_IDENTIFIER] + [by_template.get(t) for t in EXECUTION_TEMPLATES]
    for parent, child in zip(chain, chain[1:]):
        if child is None:
            continue
        edge = edge_type(nodes, child, parent)
        if edge is None:
            errs.append(f"{child!r} does not have {parent!r} as a parent")
        elif edge != EXECUTION_EDGE:
            errs.append(
                f"{child!r} follows {parent!r} through a {edge!r} edge, "
                f"expected {EXECUTION_EDGE!r}"
            )

    # No execution node may have an alternate parent that bypasses approval.
    for template in EXECUTION_TEMPLATES:
        execution = by_template.get(template)
        if execution is None:
            continue
        for entry in nodes[execution]["parents"]:
            if APPROVAL_IDENTIFIER not in ancestors(nodes, entry["identifier"]) \
                    and entry["identifier"] != APPROVAL_IDENTIFIER:
                errs.append(
                    f"execution node {template!r} has parent {entry['identifier']!r}, "
                    f"which does not pass through the approval node"
                )

    return errs


def pre_approval(nodes: dict[str, dict]) -> set[str]:
    """Nodes that are not the approval node and are not downstream of it."""
    approvals = {i for i, n in nodes.items() if n["is_approval"]}
    downstream, changed = set(approvals), True
    while changed:
        changed = False
        for ident, node in nodes.items():
            if ident not in downstream and any(p in downstream for p in parent_ids(node)):
                downstream.add(ident)
                changed = True
    return {i for i in nodes if i not in downstream}


def check() -> list[str]:
    nodes = workflow_nodes(ROOT / "controller" / "workflow_templates.yml")
    templates = job_template_playbooks(ROOT / "controller" / "job_templates.yml")

    # Graph shape first: membership checks on a cyclic or disconnected graph
    # would report confusing secondary failures.
    errs = validate_graph(nodes)
    if errs:
        return errs

    readiness = sorted(
        nodes[i]["template"] for i in pre_approval(nodes) if nodes[i]["template"]
    )

    errs += validate_stages(nodes, pre_approval(nodes))
    errs += validate_edge_types(nodes)

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
          f"{len(REQUIRED_CAPABILITIES)} readiness capabilities; "
          f"branch->report edges are {BRANCH_TO_REPORT_EDGE}, "
          f"report->approval is {REPORT_TO_APPROVAL_EDGE}, "
          f"approval->execution is {EXECUTION_EDGE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
