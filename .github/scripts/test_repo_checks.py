#!/usr/bin/env python3
"""Unit tests for repo_checks.py.

Each test builds a small repository on disk, introduces exactly one defect, and
asserts the checker reports it. Tests are weighted towards cases where a check
could silently pass, since a check that cannot fail provides no signal. The
final tests assert that a clean tree and legitimate path spellings stay quiet.

  python3 .github/scripts/test_repo_checks.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import repo_checks as pc  # noqa: E402

GOOD_README = textwrap.dedent("""\
    # Test

    Install runtime collections, then build:

    ```bash
    ansible-builder build --container-runtime podman \\
      -f execution-environment/execution-environment.yml \\
      -t ee-airgap-readiness:1.0 \\
      --build-arg PYCMD=/usr/bin/python3.12 \\
      --extra-build-cli-args="--platform linux/amd64"
    ```

    Applying controller-as-code needs controller/requirements.yml.
    """)

GOOD_AGENT = textwrap.dedent("""\
    hosts:
      - hostname: master-0
        interfaces:
          - macAddress: "52:54:00:de:00:01"
    """)


def build_repo(tmp: Path) -> Path:
    """A minimal repository that passes every check."""
    root = tmp / "repo"
    (root / "demo/bundle").mkdir(parents=True)
    (root / "demo/dns").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)
    (root / "execution-environment").mkdir(parents=True)

    (root / "README.md").write_text(GOOD_README)
    (root / "demo/bundle/install-config.yaml").write_text("baseDomain: lab.example.com\n")
    (root / "demo/bundle/agent-config.yaml").write_text(GOOD_AGENT)
    (root / "demo/bundle/imageset-config.yaml").write_text("kind: ImageSetConfiguration\n")
    (root / "demo/dns/passing.yml").write_text("zone: {}\n")
    (root / "demo/dns/failing_dns.yml").write_text("zone: {}\n")
    (root / "docs/DEMO.md").write_text("# Demo\n")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-qm", "init"], cwd=root, check=True)
    return root


results: list[tuple[str, bool, str]] = []


def case(name: str, mutate, expect_substr: str | None) -> None:
    """Apply one mutation; assert the expected failure appears (or none does)."""
    with tempfile.TemporaryDirectory() as td:
        root = build_repo(Path(td))
        mutate(root)
        try:
            failures = pc.run_checks(root)
        except subprocess.CalledProcessError:
            failures = ["__git_failed__"]
        if expect_substr is None:
            ok = not failures
            detail = "clean" if ok else f"unexpected: {failures}"
        else:
            ok = any(expect_substr in f for f in failures)
            detail = "caught" if ok else f"MISSED — got {failures or 'nothing'}"
        results.append((name, ok, detail))


# --- cases where a weaker check would pass ---------------------------------

def _wrong_path_matching_basename(root: Path) -> None:
    # A real DEMO.md exists at docs/DEMO.md. A link to docs/old/DEMO.md must not
    # pass merely because the basename matches somewhere else.
    (root / "docs/DEMO.md").write_text("See [guide](../docs/old/DEMO.md)\n")


def _doc_mention_wrong_path(root: Path) -> None:
    (root / "docs/DEMO.md").write_text("Refer to `docs/old/DEMO.md` for detail.\n")


def _empty_pull_secret(root: Path) -> None:
    p = root / "demo/bundle/install-config.yaml"
    p.write_text(p.read_text() + "pullSecret: ''\n")


def _empty_ssh_key(root: Path) -> None:
    p = root / "demo/bundle/install-config.yaml"
    p.write_text(p.read_text() + 'sshKey: ""\n')


def _pem_in_fixture(root: Path) -> None:
    p = root / "demo/bundle/install-config.yaml"
    p.write_text(p.read_text() + "cert: |\n  -----BEGIN CERTIFICATE-----\n  AAAA\n")


def _flag_only_in_prose(root: Path) -> None:
    # The flag is mentioned in the document but missing from the command.
    text = GOOD_README.replace(
        "  --build-arg PYCMD=/usr/bin/python3.12 \\\n", "")
    text += "\nRemember to pass --build-arg PYCMD=/usr/bin/python3.12 when building.\n"
    (root / "README.md").write_text(text)


def _invalid_mac(root: Path) -> None:
    (root / "demo/bundle/agent-config.yaml").write_text(
        GOOD_AGENT.replace("52:54:00:de:00:01", "52:54:00:de:m0:01"))


def _mention_escapes_root(root: Path) -> None:
    # `../../README.md` written inside docs/ resolves above the repository root.
    # It may exist on the author's machine; a reader has nothing there.
    (root / "docs/DEMO.md").write_text("Config lives in `../../README.md`.\n")


def _link_escapes_root(root: Path) -> None:
    (root / "docs/DEMO.md").write_text("See [readme](../../README.md)\n")


def _link_written_root_relative_without_slash(root: Path) -> None:
    # docs/DEMO.md linking to "docs/DEMO.md" means docs/docs/DEMO.md to a
    # browser: broken on GitHub, even though the same string resolves from the
    # repository root.
    (root / "docs/DEMO.md").write_text("See [self](docs/DEMO.md)\n")


# --- false-FAILURE cases: legitimate references must stay quiet ------------

def _mention_root_relative(root: Path) -> None:
    (root / "docs/DEMO.md").write_text("Start from `README.md`, then `docs/DEMO.md`.\n")


def _mention_relative_upward(root: Path) -> None:
    # Written inside docs/, `../README.md` is the root README. Legitimate.
    (root / "docs/DEMO.md").write_text("Build instructions are in `../README.md`.\n")


def _link_relative_upward(root: Path) -> None:
    (root / "docs/DEMO.md").write_text("See [readme](../README.md)\n")


def _link_root_absolute(root: Path) -> None:
    (root / "docs/DEMO.md").write_text("See [readme](/README.md)\n")


def _link_anchor_and_query_stripped(root: Path) -> None:
    (root / "docs/DEMO.md").write_text(
        "See [a](../README.md#build) and [b](#local-anchor) and [c](../README.md?plain=1)\n")


def _untracked_fixture(root: Path) -> None:
    subprocess.run(["git", "rm", "-q", "--cached", "demo/dns/passing.yml"],
                   cwd=root, check=True)


def _git_unavailable(root: Path) -> None:
    shutil.rmtree(root / ".git")


case("wrong markdown path, matching basename elsewhere", _wrong_path_matching_basename, "markdown-link")
case("doc mention at a path that does not exist",        _doc_mention_wrong_path,       "doc-reference")
case("empty pullSecret still rejected",                  _empty_pull_secret,            "fixture-secret")
case("empty sshKey still rejected",                      _empty_ssh_key,                "fixture-secret")
case("PEM material in fixture",                          _pem_in_fixture,               "fixture-secret")
case("required build flag only in prose",                _flag_only_in_prose,           "readme-build")
case("invalid MAC address",                              _invalid_mac,                  "fixture-mac")
case("fixture untracked by git",                         _untracked_fixture,            "fixture-tracked")
case("git command failure is not a silent pass",         _git_unavailable,              "__git_failed__")

# Path resolution: references that escape the repository, or that resolve only
# from the wrong base, must not count as resolved.
case("backticked mention escaping the repository root",  _mention_escapes_root,         "outside the repository")
case("markdown link escaping the repository root",       _link_escapes_root,            "outside the repository")
case("link written root-relative without a slash",       _link_written_root_relative_without_slash, "markdown-link")

# Legitimate spellings must stay quiet, or the checks above become noise.
case("root-relative mention accepted",                   _mention_root_relative,        None)
case("upward mention inside the tree accepted",          _mention_relative_upward,      None)
case("upward link inside the tree accepted",             _link_relative_upward,         None)
case("root-absolute link accepted",                      _link_root_absolute,           None)
case("anchors and queries stripped before resolving",    _link_anchor_and_query_stripped, None)

case("clean repository reports nothing",                 lambda r: None,                None)

width = max(len(n) for n, _, _ in results)
failed = 0
print("repo_checks unit tests\n")
for name, ok, detail in results:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed += 0 if ok else 1

print()
if failed:
    print(f"{failed} of {len(results)} tests failed.")
    sys.exit(1)
print(f"All {len(results)} tests passed.")
