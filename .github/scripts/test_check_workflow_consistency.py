#!/usr/bin/env python3
"""Unit tests for check_workflow_consistency.py.

Two things are being proved:

  * a coordinated edit cannot remove a required readiness capability, however
    many files are changed together;
  * legal reformatting of the Controller YAML does not blind the checker, which
    the previous regex-based reader was vulnerable to.

  python3 .github/scripts/test_check_workflow_consistency.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ".github/scripts/check_workflow_consistency.py"

WORKFLOW = "controller/workflow_templates.yml"
TEMPLATES = "controller/job_templates.yml"
HARNESS = "demo/node-isolation-test.sh"
ASSERTION = ".github/scripts/assert_isolation_summary.py"

# Exact fixture text, kept as constants so quoting stays readable.
WF_VIPS = '        - "13-validate-vips"'
JT_VIPS = ('        - { name: "13-validate-vips",     '
           'playbook: "playbooks/13_validate_vips.yml",        '
           'credentials: ["bastion-ssh"] }')
HARNESS_VIPS = "  13_validate_vips\n"
ASSERT_VIPS = '    "13_validate_vips",\n'


def run_in(root: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(root / SCRIPT)], capture_output=True, text=True, cwd=root
    )
    return proc.returncode, proc.stdout + proc.stderr


def sandbox(tmp: Path) -> Path:
    root = tmp / "repo"
    for rel in (".github/scripts", "controller", "demo"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel in (SCRIPT, ASSERTION, WORKFLOW, TEMPLATES, HARNESS):
        shutil.copy(ROOT / rel, root / rel)
    return root


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture text not found in {path.name}: {old[:70]!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


results: list[tuple[str, bool, str]] = []


def case(name: str, mutate, expect_failure: bool) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = sandbox(Path(td))
        mutate(root)
        rc, out = run_in(root)
        ok = (rc != 0) == expect_failure
        detail = "caught" if expect_failure else "clean"
        results.append((name, ok, detail if ok else f"rc={rc} {out.strip()[:120]}"))


case("unmodified repository is consistent", lambda r: None, False)


def _coordinated_removal(root: Path) -> None:
    """Delete 13-validate-vips from every file a maintainer could edit."""
    edit(root / HARNESS, HARNESS_VIPS, "")
    edit(root / ASSERTION, ASSERT_VIPS, "")
    edit(root / WORKFLOW, WF_VIPS + "\n", "")
    edit(root / TEMPLATES, JT_VIPS + "\n", "")


case("coordinated removal from every editable file still fails",
     _coordinated_removal, True)

case("node removed from the harness only fails",
     lambda r: edit(r / HARNESS, HARNESS_VIPS, ""), True)
case("node removed from the CI assertion only fails",
     lambda r: edit(r / ASSERTION, ASSERT_VIPS, ""), True)
case("extra node in the harness fails",
     lambda r: edit(r / HARNESS, "  20_readiness_report\n",
                    "  20_readiness_report\n  99_surprise\n"), True)
case("workflow node removed while the lists keep it fails",
     lambda r: edit(r / WORKFLOW, WF_VIPS + "\n", ""), True)
case("a job template pointing at a different playbook fails",
     lambda r: edit(r / TEMPLATES, 'playbook: "playbooks/13_validate_vips.yml"',
                    'playbook: "playbooks/13_validate_other.yml"'), True)
case("a renamed job template fails",
     lambda r: edit(r / TEMPLATES, 'name: "13-validate-vips"',
                    'name: "13-validate-vip"'), True)
case("a workflow node with no job template fails",
     lambda r: edit(r / WORKFLOW, WF_VIPS, '        - "13-validate-nothing"'), True)

# Formatting variations the old regex reader could not see.
case("single-quoted workflow values are still read",
     lambda r: edit(r / WORKFLOW, WF_VIPS, "        - '13-validate-vips'"), False)
case("an extra single-quoted workflow node is detected",
     lambda r: edit(r / WORKFLOW, WF_VIPS,
                    WF_VIPS + "\n        - '99-extra-check'"), True)
case("reordered job-template keys are still read",
     lambda r: edit(r / TEMPLATES, JT_VIPS,
                    '        - { playbook: "playbooks/13_validate_vips.yml", '
                    'credentials: ["bastion-ssh"], name: "13-validate-vips" }'), False)
case("expanded block formatting is still read",
     lambda r: edit(r / TEMPLATES, JT_VIPS,
                    '        - name: "13-validate-vips"\n'
                    '          playbook: "playbooks/13_validate_vips.yml"\n'
                    '          credentials: ["bastion-ssh"]'), False)
case("single-quoted job-template values are still read",
     lambda r: edit(r / TEMPLATES, JT_VIPS,
                    "        - { name: '13-validate-vips', "
                    "playbook: 'playbooks/13_validate_vips.yml', "
                    "credentials: ['bastion-ssh'] }"), False)


# --- graph shape ------------------------------------------------------------
# Membership alone cannot prove the workflow is runnable, so these edits keep
# every capability present and break the graph instead.

DNS_PARENTS = ('        parents: [{ identifier: "validate-bundle", type: success }]\n'
               '      loop:')

case("a readiness node with no parents fails",
     lambda r: edit(r / WORKFLOW,
                    '        parents: [{ identifier: "validate-bundle", type: success }]\n      loop:',
                    "      loop:"), True)

def _cycle(root: Path) -> None:
    """Give the looped readiness nodes a parent that is one of themselves."""
    edit(root / WORKFLOW,
         '        parents: [{ identifier: "validate-bundle", type: success }]\n      loop:',
         '        parents: [{ identifier: "10-validate-dns", type: success }]\n      loop:')

case("a cycle among readiness nodes fails", _cycle, True)

case("a parent that does not exist fails",
     lambda r: edit(r / WORKFLOW, 'identifier: "validate-bundle", type: success }]',
                    'identifier: "does-not-exist", type: success }]'), True)

case("a second approval node fails",
     lambda r: edit(r / WORKFLOW, '        identifier: "approve-execute"',
                    '        identifier: "approve-twice"\n'
                    '        approval_node:\n'
                    '          name: "Second gate"\n'
                    '        parents:\n'
                    '          - { identifier: "readiness-report", type: success }\n\n'
                    '    - ansible.controller.workflow_job_template_node:\n'
                    '        <<: *ctl\n'
                    '        workflow: "openshift-site-readiness"\n'
                    '        identifier: "approve-execute"'), True)

case("a renamed approval node fails",
     lambda r: edit(r / WORKFLOW, 'identifier: "approve-execute"',
                    'identifier: "authorise"'), True)

case("the report detached from a branch fails",
     lambda r: edit(r / WORKFLOW, '          - { identifier: "13-validate-vips",     type: always }\n', ""), True)

case("approval attached to a readiness branch instead of the report fails",
     lambda r: edit(r / WORKFLOW, '          - { identifier: "readiness-report", type: success }',
                    '          - { identifier: "10-validate-dns", type: success }'), True)

case("a readiness branch moved behind the approval gate fails",
     lambda r: edit(r / WORKFLOW,
                    '        parents: [{ identifier: "validate-bundle", type: success }]\n      loop:',
                    '        parents: [{ identifier: "approve-execute", type: success }]\n      loop:'), True)

case("an execution node bypassing approval fails",
     lambda r: edit(r / WORKFLOW,
                    'unified_job_template: "30-bmc-prepare-hosts"\n'
                    '        parents: [{ identifier: "approve-execute", type: success }]',
                    'unified_job_template: "30-bmc-prepare-hosts"\n'
                    '        parents: [{ identifier: "readiness-report", type: success }]'), True)

# --- multiple plays and duplicates ------------------------------------------
case("a workflow node in a SECOND play is not ignored",
     lambda r: (r / WORKFLOW).write_text(
         (r / WORKFLOW).read_text()
         + "\n- name: A second play the checker must not ignore\n"
           "  hosts: localhost\n"
           "  connection: local\n"
           "  gather_facts: false\n"
           "  tasks:\n"
           "    - ansible.controller.workflow_job_template_node:\n"
           '        workflow: "openshift-site-readiness"\n'
           '        identifier: "99-sneaky"\n'
           '        unified_job_template: "99-sneaky"\n'
           '        parents: [{ identifier: "validate-bundle", type: success }]\n',
         encoding="utf-8"), True)

case("a duplicate workflow node identifier fails",
     lambda r: edit(r / WORKFLOW, '        identifier: "intake"',
                    '        identifier: "readiness-report"'), True)

case("a duplicate job-template name fails",
     lambda r: edit(r / TEMPLATES, 'name: "14-validate-bastion"',
                    'name: "13-validate-vips"'), True)

width = max(len(n) for n, _, _ in results)
failed = 0
print("check_workflow_consistency unit tests\n")
for name, ok, detail in results:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed += 0 if ok else 1

print()
if failed:
    print(f"{failed} of {len(results)} tests failed.")
    sys.exit(1)
print(f"All {len(results)} tests passed.")
