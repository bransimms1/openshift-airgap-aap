#!/usr/bin/env python3
"""Fail the build if a literal credential was committed.

Credentials belong in Automation Controller, not in Git. A referenced value -
`{{ bmc_password }}`, `${TOKEN}`, `${{ secrets.X }}` - is the credential being
injected at run time and is what this repository argues for. A literal value in
a credential-shaped key is not.

gitleaks runs alongside this and covers the general case: cloud keys, tokens,
PEM material, kubeconfigs. This check exists for the project's own convention,
which a general scanner has no way to know about.
"""
import re
import sys
from pathlib import Path

SCAN_SUFFIXES = {".yml", ".yaml", ".json", ".cfg", ".sh"}
SKIP_DIRS = {".git", "context", "collections"}

KEY_VALUE = re.compile(r"^\s*[-\s]*(?P<key>[A-Za-z0-9_.-]+)\s*[:=]\s*(?P<value>.+?)\s*$")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

CREDENTIAL_WORDS = {"password", "passwd", "token", "secret", "credential", "apikey"}
CREDENTIAL_PAIRS = {("api", "key"), ("secret", "key"), ("access", "key"), ("private", "key")}

# Values that reference a credential rather than containing one, plus the
# scalars that describe a field instead of holding a value: `secret: true` in a
# credential type marks a field sensitive.
SAFE = re.compile(
    r"""^["']?\s*(\{\{.*\}\}|\$\{.*\}|\$[A-Za-z_]\w*|\$\(.*\)"""
    r"""|true|false|yes|no|string|boolean|integer|\d+|omit|null|~|)\s*["']?$""",
    re.IGNORECASE | re.DOTALL,
)


def is_credential_key(key: str) -> bool:
    words = re.split(r"[_.\-]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower())
    if any(w in CREDENTIAL_WORDS for w in words):
        return True
    return any(pair in CREDENTIAL_PAIRS for pair in zip(words, words[1:]))


def main() -> int:
    findings = []
    for path in Path(".").rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if set(path.parts) & SKIP_DIRS:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(lines, 1):
            if PRIVATE_KEY.search(line):
                findings.append((path, n, "private key material"))
                continue
            m = KEY_VALUE.match(line)
            if m and is_credential_key(m.group("key")):
                value = m.group("value").split("#")[0].strip()
                if not SAFE.match(value):
                    findings.append((path, n, line.strip()))

    for path, n, detail in findings:
        print(f"::error file={path},line={n}::Possible credential: {detail}")
    if findings:
        print(f"\n{len(findings)} possible credential(s) committed. "
              "Use an AAP credential and reference it as a variable instead.")
        return 1
    print("No literal credentials found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
