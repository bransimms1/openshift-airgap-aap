#!/usr/bin/env bash
# Negative tests for the repository's secret-scanning rules.
#
# Covers:
#   * gitleaks              — the broad scanner, via its JSON report
#   * secret_scan.py        — the project-specific scanner, via its exit code
#                             and structured output
#
# Assertions check the reported finding, not merely a non-zero exit: the
# expected rule must have fired at the expected path. A scanner that fails on a
# configuration error also exits non-zero, so exit status alone is not evidence
# of detection.
#
# Injected payloads are assembled at run time from fragments and random bytes,
# so no credential-shaped literal is committed. Every mutation is made to a
# throwaway copy; the repository is never modified.
#
#   .github/scripts/negative_secret_tests.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
( cd "$REPO" && git archive HEAD ) | tar -x -C "$WORK"

command -v gitleaks >/dev/null 2>&1 || { echo "gitleaks not installed"; exit 1; }
command -v python3  >/dev/null 2>&1 || { echo "python3 not installed";  exit 1; }

# Payloads built at run time - never written literally into this file.
PREFIX="$(printf 'gh%s_' 'p')"
TOKEN="${PREFIX}$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 36)"
DASHES="$(printf -- '-----')"
PEM="${DASHES}BEGIN RSA PRIVATE KEY${DASHES}
$(LC_ALL=C tr -dc 'A-Za-z0-9+/' </dev/urandom | head -c 64)
${DASHES}END RSA PRIVATE KEY${DASHES}"
LITERAL_PW="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)"

pass=0; fail=0
ok()   { printf '  ok    %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  FAIL  %s — %s\n' "$1" "$2"; fail=$((fail+1)); }

sandbox() { local d; d="$(mktemp -d)"; cp -R "$WORK/." "$d/"; printf '%s' "$d"; }

# --- gitleaks: injection must produce the EXPECTED finding -----------------
gl_expect() {
  local label="$1" file="$2" payload="$3" want_rule="$4"
  local dir; dir="$(sandbox)"
  printf '\n%s\n' "$payload" >> "$dir/$file"
  local rpt="$dir/.report.json"

  gitleaks dir "$dir" --config "$REPO/.gitleaks.toml" --no-banner \
      --report-format json --report-path "$rpt" >/dev/null 2>&1
  local rc=$?

  # gitleaks exits 1 for "leaks found". Any other non-zero code indicates a
  # scanner error, which must not be counted as a detection.
  if [ "$rc" -ne 1 ]; then
    bad "$label" "expected exit 1 (leaks found), got $rc"
    rm -rf "$dir"; return
  fi
  if [ ! -s "$rpt" ]; then
    bad "$label" "no report produced (rc=$rc) — scanner error, not a detection"
    rm -rf "$dir"; return
  fi
  python3 - "$rpt" "$file" "$want_rule" <<'PY'
import json, sys
rpt, want_file, want_rule = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(rpt))
hits = [f for f in data if f.get("File","").endswith(want_file)
        and f.get("RuleID") == want_rule]
sys.exit(0 if hits else 1)
PY
  if [ $? -eq 0 ]; then
    ok "$label (rule=$want_rule in $file)"
  else
    bad "$label" "report has no '$want_rule' finding for $file"
  fi
  rm -rf "$dir"
}

echo "gitleaks — injection detected with the expected rule and path"
gl_expect "token in demo fixture"       "demo/bundle/install-config.yaml"              "# $TOKEN" "github-pat"
gl_expect "token in custom scanner"     ".github/scripts/secret_scan.py"               "# $TOKEN" "github-pat"
gl_expect "token in credential type"    "controller/credential_types/bmc_redfish.yml"  "# $TOKEN" "github-pat"
gl_expect "private key in demo fixture" "demo/bundle/install-config.yaml"              "$PEM"     "private-key"

# clean tree must be quiet, or every assertion above is meaningless
rpt="$WORK/.clean.json"
gitleaks dir "$WORK" --config "$REPO/.gitleaks.toml" --no-banner \
    --report-format json --report-path "$rpt" >/dev/null 2>&1
clean_rc=$?
clean_n="$(python3 -c "
import json,os,sys
p='$rpt'
sys.stdout.write(str(len(json.load(open(p)))) if os.path.exists(p) and os.path.getsize(p) else '0')
" 2>/dev/null || echo ERR)"
# Exit 0 is required, not merely an empty report: a scanner that fails before
# writing anything also leaves an empty report behind.
if [ "$clean_rc" -ne 0 ]; then
  bad "clean tree is quiet" "expected exit 0 on a clean tree, got $clean_rc"
elif [ "$clean_n" != "0" ]; then
  bad "clean tree is quiet" "gitleaks reported $clean_n finding(s) on an unmodified tree"
else
  ok "clean tree is quiet"
fi

# --- secret_scan.py: detections ------------------------------------------
echo
echo "secret_scan.py — literals detected"
# Asserts the structured finding rather than the presence of a filename in the
# output. secret_scan.py prints
# "::error file=<path>,line=<n>::Possible credential: ...". The expected line is
# the line the payload was appended to, so a finding on an unrelated line of the
# same file does not satisfy the assertion.
css_expect() {
  local label="$1" file="$2" payload="$3"
  local dir; dir="$(sandbox)"
  printf '\n%s\n' "$payload" >> "$dir/$file"
  local want_line; want_line="$(awk 'END{print NR}' "$dir/$file")"
  local out; out="$( cd "$dir" && python3 .github/scripts/secret_scan.py 2>&1 )"
  local rc=$?
  if [ "$rc" -ne 1 ]; then
    bad "$label" "expected exit 1 (literal detected), got $rc"
  elif ! printf '%s\n' "$out" | grep -qF "::error file=${file},line=${want_line}::Possible credential:"; then
    bad "$label" "no structured finding for ${file}:${want_line} — got: $(printf '%s' "$out" | head -2 | tr '\n' ' ')"
  else
    ok "$label (${file}:${want_line})"
  fi
  rm -rf "$dir"
}

css_expect "literal password in YAML"       "inventories/demo/group_vars/all/demo.yml" "  bmc_password: ${LITERAL_PW}"
css_expect "literal token in a workflow"    ".github/workflows/ci.yml"                 "  token: ${LITERAL_PW}"
css_expect "literal token in a shell file"  ".github/scripts/negative_secret_tests.sh" "GITHUB_TOKEN=${LITERAL_PW}"
css_expect "literal in credential type"     "controller/credential_types/bmc_redfish.yml" "  password: ${LITERAL_PW}"

# Mixed literal-and-reference values. A value is only safe when it is built
# entirely from references; a literal fragment anywhere makes it a finding.
DOLLAR='$'
css_expect "literal glued after a jinja ref"  "inventories/demo/group_vars/all/demo.yml" "  bmc_password: \"{{ bmc_password }}${LITERAL_PW}\""
css_expect "literal glued after a shell ref"  "inventories/demo/group_vars/all/demo.yml" "  bmc_token: \"${DOLLAR}{TOKEN}${LITERAL_PW}\""
css_expect "literal glued before a shell ref" "inventories/demo/group_vars/all/demo.yml" "  bmc_password: ${LITERAL_PW}${DOLLAR}{TOKEN}"

# Shell parameter expansion carrying a literal default. The default is the
# value whenever the variable is unset, so it is a committed literal.
for op in ':-' '-' ':=' ':+' ':?'; do
  css_expect "shell expansion default '${op}'" "inventories/demo/group_vars/all/demo.yml" \
    "  bmc_password: \"${DOLLAR}{BMC_PASSWORD${op}${LITERAL_PW}}\""
done

# Multi-segment key names.
css_expect "multi-segment prefix, password"   "inventories/demo/group_vars/all/demo.yml" "  database_admin_password: ${LITERAL_PW}"
css_expect "multi-segment prefix, token"      "inventories/demo/group_vars/all/demo.yml" "  prod_github_token: ${LITERAL_PW}"
css_expect "multi-segment prefix, secret key" "inventories/demo/group_vars/all/demo.yml" "  service_api_secret_key: ${LITERAL_PW}"
css_expect "access_key"                       "inventories/demo/group_vars/all/demo.yml" "  access_key: ${LITERAL_PW}"
css_expect "private_key"                      "inventories/demo/group_vars/all/demo.yml" "  private_key: ${LITERAL_PW}"

# camelCase credential keys, which must be matched as well as the separated
# spellings.
css_expect "camelCase apiToken"       "inventories/demo/group_vars/all/demo.yml" "  apiToken: ${LITERAL_PW}"
css_expect "camelCase bmcPassword"    "inventories/demo/group_vars/all/demo.yml" "  bmcPassword: ${LITERAL_PW}"
css_expect "camelCase clientSecret"   "inventories/demo/group_vars/all/demo.yml" "  clientSecret: ${LITERAL_PW}"
css_expect "camelCase adminPassword"  "inventories/demo/group_vars/all/demo.yml" "  adminPassword: ${LITERAL_PW}"
css_expect "camelCase accessKeyId"    "inventories/demo/group_vars/all/demo.yml" "  accessKeyId: ${LITERAL_PW}"

# --- secret_scan.py: references must NOT be flagged -----------------------
echo
echo "secret_scan.py — references accepted, not flagged"
css_accept() {
  local label="$1" payload="$2"
  local dir; dir="$(sandbox)"
  printf '\n%s\n' "$payload" >> "$dir/inventories/demo/group_vars/all/demo.yml"
  local out; out="$( cd "$dir" && python3 .github/scripts/secret_scan.py 2>&1 )"
  local rc=$?
  # Exit 0 and the completed-scan message: a scanner that failed before
  # scanning would also not report this line as a finding.
  if [ "$rc" -ne 0 ]; then
    bad "$label" "reference was wrongly flagged as a literal (exit $rc)"
  elif ! printf '%s\n' "$out" | grep -qF "No literal credentials found."; then
    bad "$label" "exit 0 but the scanner did not report a completed clean scan"
  else
    ok "$label"
  fi
  rm -rf "$dir"
}
css_accept 'jinja {{ bmc_password }}'      '  bmc_password: "{{ bmc_password }}"'
css_accept 'shell ${PASSWORD}'             '  bmc_password: "${PASSWORD}"'
css_accept 'shell $PASSWORD'               '  bmc_password: "$PASSWORD"'
css_accept 'actions secret expression'     '  bmc_password: "${{ secrets.BMC_PASSWORD }}"'
css_accept 'concatenated shell refs'       '  bmc_password: "${PREFIX}${BODY}"'
css_accept 'two jinja refs'                '  bmc_password: "{{ a }} {{ b }}"'
css_accept 'nested jinja emitting a template' '  bmc_password: "{{ '"'"'{{ bmc_password }}'"'"' }}"'
css_accept 'sensitive-field declaration'   '  secret: true'
css_accept 'braced ref with no operator'   '  bmc_password: "${BMC_PASSWORD}"'
# Words that merely contain a credential word must not be flagged. This scanner
# is deliberately narrow.
css_accept 'tokenizer is not a token'      '  tokenizer: simple'  # literal value
css_accept 'keyValue is not a key'         '  keyValue: plain'
# Values chosen so the only way these pass is the key not matching. A safe
# scalar such as 'true' would be accepted regardless of the key.
css_accept 'passwordless is not a password' '  passwordless: enabled'

# clean tree must pass the custom scanner too
clean_out="$( cd "$WORK" && python3 .github/scripts/secret_scan.py 2>&1 )"
clean_css_rc=$?
if [ "$clean_css_rc" -ne 0 ]; then
  bad "clean tree passes secret_scan.py" "expected exit 0, got $clean_css_rc"
elif ! printf '%s\n' "$clean_out" | grep -qF "No literal credentials found."; then
  bad "clean tree passes secret_scan.py" "exit 0 without a completed-scan message"
else
  ok "clean tree passes secret_scan.py"
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "$fail negative test(s) failed, $pass passed."
  exit 1
fi
echo "All $pass negative tests passed across both scanners."
