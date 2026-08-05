#!/usr/bin/env python3
"""Unit tests for check_workflow_consistency.py.

The defect it exists to catch is drift the two hand-maintained lists agree on:
delete a node from BOTH the harness and the CI assertion and they still match
each other, while the Controller workflow still runs it.

  python3 .github/scripts/test_check_workflow_consistency.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "‎.github/scripts/check_workflow_consistency.py".replace("‎", "")


def run_in(root: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(root / SCRIPT)], capture_output=True, text=True, cwd=root
    )
    return proc.returncode, proc.stdout + proc.stderr


def sandbox(tmp: Path) -> Path:
    root = tmp / "repo"
    for rel in (".github/scripts", "controller", "demo"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel in (
        ".github/scripts/check_workflow_consistency.py",
        ".github/scripts/assert_isolation_summary.py",
        "controller/workflow_templates.yml",
        "controller/job_templates.yml",
        "demo/node-isolation-test.sh",
    ):
        shutil.copy(ROOT / rel, root / rel)
    return root


results: list[tuple[str, bool, str]] = []


def case(name: str, mutate, expect_failure: bool) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = sandbox(Path(td))
        mutate(root)
        rc, out = run_in(root)
        ok = (rc != 0) == expect_failure
        detail = "caught" if expect_failure else "clean"
        results.append((name, ok, detail if ok else f"rc={rc} {out.strip()[:110]}"))


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture text not found in {path.name}: {old[:60]!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


case("unmodified repository is consistent", lambda r: None, False)

# The headline case: both manual lists edited together, workflow untouched.
def _drop_from_both(root: Path) -> None:
    edit(root / "demo/node-isolation-test.sh", "  13_validate_vips\n", "")
    edit(root / ".github/scripts/assert_isolation_summary.py",
         '    "13_validate_vips",\n', "")
    edit(root / ".github/scripts/check_workflow_consistency.py",
         '    "13-validate-vips": "13_validate_vips",\n', "")


case("13-validate-vips dropped from BOTH manual lists still fails",
     _drop_from_both, True)

case("node removed from the harness only fails",
     lambda r: edit(r / "demo/node-isolation-test.sh", "  13_validate_vips\n", ""), True)
case("node removed from the CI assertion only fails",
     lambda r: edit(r / ".github/scripts/assert_isolation_summary.py",
                    '    "13_validate_vips",\n', ""), True)
case("extra node in the harness fails",
     lambda r: edit(r / "demo/node-isolation-test.sh", "  20_readiness_report\n",
                    "  20_readiness_report\n  99_surprise\n"), True)
case("workflow node removed while the lists keep it fails",
     lambda r: edit(r / "controller/workflow_templates.yml",
                    '        - "13-validate-vips"\n', ""), True)
case("a job template pointing at a different playbook fails",
     lambda r: edit(r / "controller/job_templates.yml",
                    'playbook: "playbooks/13_validate_vips.yml"',
                    'playbook: "playbooks/13_validate_something_else.yml"'), True)
case("a renamed job template fails",
     lambda r: edit(r / "controller/job_templates.yml",
                    'name: "13-validate-vips"', 'name: "13-validate-vip"'), True)

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
