#!/usr/bin/env bash
# Run the readiness workflow the way Automation Controller runs it on
# OpenShift: each node a separate process, in its own filesystem, with only
# workflow artifacts crossing the boundary.
#
#   * each node is a separate ansible-playbook invocation
#   * each gets a different bundle_workdir_base, so it cannot see a directory
#     another node created
#   * set_stats output is captured and passed to the next node as extra vars
#
# A node that depends on a sibling's files fails here and passes smoke-test.yml.
#
# Usage:
#   demo/node-isolation-test.sh passing
#   demo/node-isolation-test.sh failing_dns

set -uo pipefail

SCENARIO="${1:-passing}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INV="${REPO}/inventories/demo/hosts.yml"
WORK="$(mktemp -d)"
ART="${WORK}/artifacts.json"
echo '{}' > "${ART}"

# A purpose-built config rather than the repository's: the JSON callback needs
# stdout to itself, and the repository's ansible.cfg enables profile_tasks.
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
  # Carry this node's published artifacts forward, the way Controller passes
  # workflow artifacts between nodes.
  python3 - "${out}" "${ART}" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
carried = json.load(open(sys.argv[2]))
carried.update(data.get("global_custom_stats") or {})
json.dump(carried, open(sys.argv[2], "w"), indent=2)
PY

  if [ ${rc} -eq 0 ]; then
    printf '  %-28s OK\n' "${pb}"
  else
    printf '  %-28s FAILED (rc=%s)\n' "${pb}" "${rc}"
    FAILED_NODES+=("${pb}")
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
