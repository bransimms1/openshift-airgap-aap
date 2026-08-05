#!/usr/bin/env python3
"""Fail the build if a literal credential was committed.

Credentials belong in Automation Controller, not in Git. A referenced value -
`{{ bmc_password }}`, `${TOKEN}`, `${{ secrets.X }}` - is the credential being
injected at run time and is accepted. A literal value, or a value that mixes a
reference with a literal fragment, is reported.
"""
import re
import sys
from pathlib import Path

SCAN_SUFFIXES = {".yml", ".yaml", ".json", ".cfg", ".sh"}

# .github is deliberately NOT skipped: workflow files handle secrets and can
# carry a literal like anything else.
SKIP_PARTS = {".git", "context", "node_modules"}
SKIP_SUBPATH = "collections/ansible_collections"

# Split a key=value or key: value line. Whether the KEY is credential-shaped is
# decided in is_credential_key(), not here.
#
# Key matching splits the key into words rather than using one large regex with
# a repeating prefix group. That keeps matching linear - a nested quantifier
# backtracks catastrophically on ordinary long lines - and avoids the fact that
# under re.IGNORECASE a [A-Z] class also matches lowercase, which makes
# camelCase patterns match words such as "tokenizer".
KEY_VALUE = re.compile(r"^\s*[-\s]*(?P<key>[A-Za-z0-9_.-]+)\s*[:=]\s*(?P<value>.+?)\s*$")

# A single word anywhere in the key is enough.
CREDENTIAL_WORDS = {
    "password", "passwd", "token", "secret", "credential", "credentials",
    "apikey", "pullsecret",
}

# Words that indicate a credential only next to another word. "key" alone
# matches too much ordinary YAML - keyValue, access_mode, keys - so it counts
# only in these pairings.
CREDENTIAL_PAIRS = {
    ("api", "key"), ("secret", "key"), ("access", "key"), ("private", "key"),
    ("signing", "key"), ("pull", "secret"),
}


def key_words(key: str) -> list[str]:
    """Split a key into lowercase words, on separators AND camelCase humps.

    bmc_password       -> [bmc, password]
    prod_github_token  -> [prod, github, token]
    accessKeyId        -> [access, key, id]
    pullSecret         -> [pull, secret]
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return [w for w in re.split(r"[_.\-]+", spaced.lower()) if w]


def is_credential_key(key: str) -> bool:
    words = key_words(key)
    if any(w in CREDENTIAL_WORDS for w in words):
        return True
    return any(pair in CREDENTIAL_PAIRS for pair in zip(words, words[1:]))

PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

# Booleans, numbers and bare type words are declarations, not credentials:
# "secret: true" in an AAP credential-type marks a field as sensitive, and
# "type: string" describes it.
SAFE_SCALAR = re.compile(
    r"""^(true|false|yes|no|on|off|string|boolean|integer|\d+|omit|null|~|)$""",
    re.IGNORECASE,
)

# Reference tokens that need no brace counting.
#
# The Jinja and GitHub Actions forms are handled by _match_braced instead,
# because they nest: an AAP injector value such as "{{ '{{ bmc_password }}' }}"
# would stop a non-greedy pattern at the inner "}}" and leave the remainder
# looking like a committed literal.
#
# The braced shell form accepts a bare name only. "${TOKEN}" is a reference;
# "${TOKEN:-value}" also commits a literal default, which is the value whenever
# TOKEN is unset, so the operator forms (:- - := :+ :? and the substitution
# operators) are not accepted.
#
# $(...) is accepted: what is committed there is a command, not a value.
REFERENCE_TOKEN = re.compile(
    r"""
      \{%.*?%\}                      # {% ... %}          Jinja statement
    | \$\{[A-Za-z_][A-Za-z0-9_]*\}   # ${TOKEN}           shell, braced, bare name
    | \$\([^()]*\)                   # $(command)         command substitution
    | \$[A-Za-z_][A-Za-z0-9_]*       # $TOKEN             shell, bare
    """,
    re.VERBOSE | re.DOTALL,
)


def _match_braced(value: str, pos: int) -> int | None:
    """End index of a `{{ ... }}` or `${{ ... }}` starting at `pos`, else None.

    Counts nesting, so "{{ '{{ x }}' }}" is consumed whole while "{{ a }}abc"
    consumes only "{{ a }}" and leaves the literal to be reported.
    """
    start = pos + 1 if value.startswith("${{", pos) else pos
    if not value.startswith("{{", start):
        return None
    depth, i = 1, start + 2
    while i < len(value):
        if value.startswith("{{", i):
            depth += 1
            i += 2
        elif value.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return None


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def is_all_references(value: str) -> bool:
    """True only when every character of the value belongs to a reference.

    Reference tokens are consumed from the front of the value and anything left
    over makes it a finding, so a literal glued to a reference is reported:

        password: "{{ password }}abc"
        token: "${TOKEN}suffix"
        password: prefix${TOKEN}

    Values built entirely from references - "${PREFIX}${BODY}",
    "{{ a }} {{ b }}" - are accepted.
    """
    pos = 0
    consumed_one = False
    while pos < len(value):
        if value[pos].isspace():
            pos += 1
            continue
        end = _match_braced(value, pos)
        if end is None:
            m = REFERENCE_TOKEN.match(value, pos)
            if not m:
                return False
            end = m.end()
        pos = end
        consumed_one = True
    return consumed_one


def is_safe(value: str) -> bool:
    value = value.split("#")[0].strip()
    value = _strip_quotes(value).strip()
    if SAFE_SCALAR.match(value):
        return True
    # A lookup as the whole value is a reference resolved at run time. Anything
    # around it is not, so the same all-or-nothing rule applies.
    if re.fullmatch(r"lookup\(.*\)", value, re.DOTALL):
        return True
    return is_all_references(value)


def main() -> int:
    findings = []
    for path in Path(".").rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        rel = path.relative_to(Path(".")).as_posix()
        # Compare path COMPONENTS, not string prefixes: ".git" is a string
        # prefix of ".github", which would exclude every workflow file.
        parts = set(path.parts)
        if parts & SKIP_PARTS or SKIP_SUBPATH in rel:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(lines, 1):
            if PRIVATE_KEY.search(line):
                findings.append((rel, n, "private key material"))
                continue
            m = KEY_VALUE.match(line)
            if m and is_credential_key(m.group("key")) and not is_safe(m.group("value")):
                findings.append((rel, n, line.strip()))

    if findings:
        for rel, n, detail in findings:
            print(f"::error file={rel},line={n}::Possible credential: {detail}")
        print(
            f"\n{len(findings)} possible credential(s) committed. "
            "Use an AAP credential and reference it as a variable instead."
        )
        return 1

    print("No literal credentials found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
