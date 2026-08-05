#!/usr/bin/env bash
# Prove every workflow node runs independently, the way AAP executes them on
# OpenShift.
#
# demo/smoke-test.yml runs the chain in ONE ansible-playbook process, which is
# convenient but dishonest: plays share a filesystem and, locally, share
# nothing else. Automation Controller does the opposite — each node is a
# separate process in its own pod, and the ONLY thing that crosses the boundary
# is workflow artifacts (set_stats).
#
# This script reproduces that faithfully:
#   * each node is a separate ansible-playbook invocation
#   * each node gets a DIFFERENT bundle_workdir_base, so it cannot see any
#     directory a previous node created
#   * set_stats output is captured and fed to the next node as extra vars,
#     which is exactly what Controller does with workflow artifacts
#
# Usage:
#   demo/node-isolation-test.sh passing
#   demo/node-isolation-test.sh failing_dns
#
# Set ISOLATION_SUMMARY to a path to also write a machine-readable result:
#
#   {"scenario": ..., "verdict": ..., "nodes": {"10_validate_dns": "failed"},
#    "failed_nodes": [...], "artifacts": [...]}
#
# CI asserts against that file rather than grepping node names out of the log:
# a node name appearing in the output does not prove that the node failed.
set -uo pipefail

SCENARIO="${1:-passing}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INV="${REPO}/inventories/demo/hosts.yml"
WORK="$(mktemp -d)"
ART="${WORK}/artifacts.json"
echo '{}' > "${ART}"

# A purpose-built config for this harness, rather than the repository's.
#
# The JSON callback must own stdout completely: the repository's ansible.cfg
# enables profile_tasks, whose timing lines corrupt the stream this script
# parses. Disabling it with ANSIBLE_CALLBACKS_ENABLED= works on ansible-core
# 2.15 and FAILS on 2.19+ with "A non-empty plugin name is required", because an
# empty value parses as a list containing an empty string. Generating a config
# that simply never enables the callback avoids the version difference.
CFG="${WORK}/ansible.cfg"
cat > "${CFG}" <<EOF
[defaults]
roles_path            = ${REPO}/roles
collections_path      = ${REPO}/collections:${HOME}/.ansible/collections
host_key_checking     = True
interpreter_python    = auto_silent
display_skipped_hosts = False
stdout_callback       = ansible.posix.json
EOF
export ANSIBLE_CONFIG="${CFG}"

PLAYBOOKS=(
  00_bundle_intake
  01_validate_bundle
  10_validate_dns
  11_validate_ntp
  12_validate_registry
  13_validate_vips
  14_validate_bastion
  15_validate_bmc_redfish
  20_readiness_report
)

for pb in "${PLAYBOOKS[@]}"; do
  if [ ! -f "${REPO}/playbooks/${pb}.yml" ]; then
    echo "ERROR: playbooks/${pb}.yml does not exist. The node list and the" >&2
    echo "       repository disagree; refusing to report a partial run." >&2
    exit 2
  fi
done

echo "Scenario: ${SCENARIO}"
echo "Each node runs in its own process with its own filesystem root."
echo

FAILED_NODES=()
STATUS_FILE="${WORK}/node-status.tsv"
: > "${STATUS_FILE}"
n=0
for pb in "${PLAYBOOKS[@]}"; do
  n=$((n + 1))
  # A different working-directory base per node: this is the "different pod"
  # simulation. A node that depends on a sibling's files cannot pass here.
  POD="${WORK}/pod-${n}"
  mkdir -p "${POD}"

  out="${WORK}/${pb}.json"
  ANSIBLE_SHOW_CUSTOM_STATS=1 \
  ansible-playbook -i "${INV}" "${REPO}/playbooks/${pb}.yml" \
    -e @"${ART}" \
    -e demo_scenario="${SCENARIO}" \
    -e bundle_workdir_base="${POD}" \
    > "${out}" 2>"${WORK}/${pb}.err"
  rc=$?

  # Merge this node's published artifacts into the set carried forward.
  # Invalid callback output is a harness failure, not "this node published
  # nothing". Silently substituting {} would let a broken run look like a node
  # that simply had no artifacts to share.
  if ! python3 - "${out}" "${ART}" "${pb}" <<'PY'
import json, sys
out, art, node = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.load(open(out))
except Exception as exc:
    sys.stderr.write(f"ERROR: {node} produced unparseable JSON callback output: {exc}\n")
    sys.exit(2)
stats = data.get("global_custom_stats") or {}
if not isinstance(stats, dict):
    sys.stderr.write(f"ERROR: {node} published non-mapping custom stats\n")
    sys.exit(2)
carried = json.load(open(art))
carried.update(stats)
json.dump(carried, open(art, "w"), indent=2)
PY
  then
    echo "ERROR: artifact merge failed after ${pb}; see ${WORK}" >&2
    exit 2
  fi

  if [ ${rc} -eq 0 ]; then
    printf '  %-28s OK\n' "${pb}"
    printf '%s\tok\n' "${pb}" >> "${STATUS_FILE}"
  else
    printf '  %-28s FAILED (rc=%s)\n' "${pb}" "${rc}"
    FAILED_NODES+=("${pb}")
    printf '%s\tfailed\n' "${pb}" >> "${STATUS_FILE}"
  fi
done

echo
echo "Artifacts carried across node boundaries:"
python3 -c "
import json,sys
d=json.load(open('${ART}'))
for k in sorted(d): print('   ', k)
"

echo
VERDICT=$(python3 -c "
import json
d=json.load(open('${ART}'))
print(d.get('readiness_overall','<none>'))
")
echo "Readiness verdict: ${VERDICT}"

echo
if [ ${#FAILED_NODES[@]} -eq 0 ]; then
  echo "All nodes succeeded independently."
else
  echo "Nodes that failed: ${FAILED_NODES[*]}"
fi
echo "Logs: ${WORK}"

# Machine-readable summary, for CI and anything else that needs to assert on the
# outcome rather than parse the log.
if [ -n "${ISOLATION_SUMMARY:-}" ]; then
  if ! ISO_STATUS_FILE="${STATUS_FILE}" \
       ISO_ARTIFACTS="${ART}" \
       ISO_SCENARIO="${SCENARIO}" \
       ISO_VERDICT="${VERDICT}" \
       ISO_DEST="${ISOLATION_SUMMARY}" \
       python3 - <<'SUMMARY'
import json, os, sys

nodes = {}
with open(os.environ["ISO_STATUS_FILE"]) as fh:
    for line in fh:
        if line.strip():
            name, status = line.rstrip("\n").split("\t")
            if name in nodes:
                sys.stderr.write(f"ERROR: node {name} reported twice\n")
                sys.exit(2)
            nodes[name] = status

carried = json.load(open(os.environ["ISO_ARTIFACTS"]))
json.dump({
    "scenario": os.environ["ISO_SCENARIO"],
    "verdict": os.environ["ISO_VERDICT"],
    "nodes": nodes,
    "failed_nodes": [k for k, v in nodes.items() if v == "failed"],
    "artifacts": sorted(carried),
}, open(os.environ["ISO_DEST"], "w"), indent=2)
SUMMARY
  then
    echo "ERROR: could not write the summary to ${ISOLATION_SUMMARY}" >&2
    exit 2
  fi
  echo "Summary: ${ISOLATION_SUMMARY}"
fi
