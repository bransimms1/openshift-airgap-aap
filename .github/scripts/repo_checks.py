#!/usr/bin/env python3
"""Repository consistency checks.

Gitleaks and secret_scan.py cover credentials. This is the documentation and
fixture layer: it enforces rules specific to this repository that no general
tool knows about.

  * documentation links and backticked path references resolve
  * the demo fixtures are tracked, so a fresh clone can run demo mode
  * fixture MAC addresses are syntactically valid
  * fixtures carry nothing credential-shaped
  * the README build command carries the flags the build requires

Run directly, or import `run_checks(root)` from a test.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Names appearing in prose that describe artefacts OUTSIDE this repository -
# files found inside an Airgap Architect export, not documents shipped here.
EXTERNAL_DOCS = {"FIELD_MANUAL.md"}

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
DOC_MENTION_RE = re.compile(r"`([\w./-]+\.md)`")

REQUIRED_FIXTURES = (
    "demo/bundle/install-config.yaml",
    "demo/bundle/agent-config.yaml",
    "demo/bundle/imageset-config.yaml",
    "demo/dns/passing.yml",
    "demo/dns/failing_dns.yml",
)

# Keys that must not appear in a public fixture AT ALL - not even empty. An
# empty pullSecret still tells a reader the fixture is shaped to carry one.
FORBIDDEN_FIXTURE_KEYS = ("pullSecret", "sshKey")

CREDENTIAL_SHAPES = (
    ("-----BEGIN CERTIFICATE-----", "PEM certificate"),
    ("PRIVATE KEY-----", "private key"),
    ("client-certificate-data:", "kubeconfig client certificate"),
    ("client-key-data:", "kubeconfig client key"),
    ("Authorization: Bearer", "bearer token"),
    ('"auths":', "registry auth JSON"),
)

REQUIRED_BUILD_TOKENS = (
    "-f execution-environment/execution-environment.yml",
    "-t ee-airgap-readiness",
    "--build-arg PYCMD=/usr/bin/python3.12",
    '--extra-build-cli-args="--platform linux/amd64"',
)


def tracked_files(root: Path) -> set[str]:
    """Files git actually tracks. Raises if git fails, so a git error cannot
    make the fixture-tracked check pass for the wrong reason."""
    proc = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    )
    return set(proc.stdout.split())


def _markdown_files(root: Path) -> list[Path]:
    """Only TRACKED markdown. An untracked or gitignored file is not part of
    the published repository, and holding it to these rules would produce
    failures a reader of the repository could never observe."""
    try:
        tracked = tracked_files(root)
    except subprocess.CalledProcessError:
        raise
    return [root / f for f in sorted(tracked)
            if f.endswith(".md") and (root / f).is_file()]


def _inside(root: Path, candidate: Path) -> bool:
    """True when `candidate` lands inside the repository.

    A reference is only meaningful if it names something the repository ships.
    A chain of `../` deep enough to leave the tree may resolve on the author's
    machine and to nothing for a reader, so it does not count as resolved.
    Compared after resolve() on both sides so symlinked temporary directories
    do not produce a spurious mismatch.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _resolve_reference(root: Path, md: Path, target: str, *, mode: str) -> tuple[bool, bool]:
    """Resolve one path reference found in `md`. Returns (resolved, escaped).

    One resolver serves both checks so their path handling cannot drift apart.
    Two modes, because the two kinds of reference are read differently:

      mode="link"    a markdown link. A reader clicks it, so it follows the
                     rules a browser follows: relative to the containing file,
                     or root-relative when it starts with "/". `docs/DEMO.md`
                     written inside docs/ is a broken link on GitHub and is
                     reported as one here.

      mode="mention" a path named in backticks in prose. Authors write these
                     from the root ("see `docs/DEMO.md`") as readily as from
                     the current directory, and both are legitimate, so either
                     resolution is accepted.

    In both modes a candidate landing outside the repository does not count as
    resolved, however real it may be on the machine running this.
    """
    if target.startswith("/"):
        candidates = [root / target.lstrip("/")]
    elif mode == "link":
        candidates = [md.parent / target]
    else:
        candidates = [md.parent / target, root / target]

    inside = [c for c in candidates if _inside(root, c)]
    if not inside:
        return False, True
    return any(c.exists() for c in inside), False


def run_checks(root: Path) -> list[str]:
    failures: list[str] = []

    def fail(check: str, detail: str) -> None:
        failures.append(f"{check}: {detail}")

    def check_reference(kind: str, md: Path, target: str, mode: str) -> None:
        resolved, escaped = _resolve_reference(root, md, target, mode=mode)
        if escaped:
            fail(kind, f"{md.relative_to(root)} -> '{target}' points outside the repository")
        elif not resolved:
            fail(kind, f"{md.relative_to(root)} -> '{target}' does not exist")

    # --- 1. markdown links resolve by PATH, not by basename ----------------
    for md in _markdown_files(root):
        text = md.read_text(encoding="utf-8", errors="ignore")
        for target in MD_LINK_RE.findall(text):
            target = target.split("#")[0].split("?")[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            check_reference("markdown-link", md, target, "link")

    # --- 2. doc mentions in backticks resolve at their stated path ---------
    for md in _markdown_files(root):
        text = md.read_text(encoding="utf-8", errors="ignore")
        for target in DOC_MENTION_RE.findall(text):
            if Path(target).name in EXTERNAL_DOCS:
                continue
            check_reference("doc-reference", md, target, "mention")

    # --- 3. demo fixtures are actually tracked ----------------------------
    files = tracked_files(root)
    for required in REQUIRED_FIXTURES:
        if required not in files:
            fail("fixture-tracked", f"{required} is untracked; a fresh clone cannot run demo mode")

    # --- 4. fixture MACs are syntactically valid ---------------------------
    agent = root / "demo/bundle/agent-config.yaml"
    if agent.exists():
        for n, line in enumerate(agent.read_text().splitlines(), 1):
            m = re.search(r"macAddress:\s*[\"']?([^\"'\s]+)", line)
            if m and not MAC_RE.match(m.group(1)):
                fail("fixture-mac", f"agent-config.yaml:{n} '{m.group(1)}' is not a valid MAC")

    # --- 5. fixtures carry nothing credential-shaped ----------------------
    demo = root / "demo"
    if demo.is_dir():
        for fixture in list(demo.rglob("*.yml")) + list(demo.rglob("*.yaml")):
            text = fixture.read_text(encoding="utf-8", errors="ignore")
            rel = fixture.relative_to(root)
            for needle, label in CREDENTIAL_SHAPES:
                if needle in text:
                    fail("fixture-secret", f"{rel} contains {label}")
            for key in FORBIDDEN_FIXTURE_KEYS:
                if re.search(rf"^\s*{key}\s*:", text, re.M):
                    fail("fixture-secret", f"{rel} declares '{key}'; it must be absent, not empty")

    # --- 6. the README build command itself carries the required flags -----
    readme = root / "README.md"
    if readme.exists():
        text = readme.read_text(encoding="utf-8", errors="ignore")
        blocks = re.findall(r"```(?:bash|sh|shell)?\n(.*?)```", text, re.S)
        build_cmds = [b for b in blocks if "ansible-builder build" in b]
        if not build_cmds:
            fail("readme-build", "no fenced block contains an 'ansible-builder build' command")
        for cmd in build_cmds:
            # Join continuations so flags split across lines still count,
            # but only within the command itself - prose does not qualify.
            joined = cmd.replace("\\\n", " ")
            for token in REQUIRED_BUILD_TOKENS:
                if token not in joined:
                    fail("readme-build", f"build command is missing {token!r}")
        if "controller/requirements.yml" not in text:
            fail("readme-build", "README does not mention controller/requirements.yml")

    return failures


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    try:
        failures = run_checks(root)
    except subprocess.CalledProcessError as exc:
        print(f"::error::git command failed ({exc.returncode}); cannot verify tracked files")
        return 2
    if failures:
        print(f"{len(failures)} repository check(s) failed:\n")
        for f in failures:
            print(f"  ::error::{f}")
        return 1
    print("All repository checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
