# Demo mode

Demo mode runs the readiness workflow end to end with **no bastion, no BMCs, no
mirror registry and no lab DNS**. It is intended for evaluating the workflow, for
development, and as a way to see what the automation produces before pointing it
at a real site.

Everything executes inside the execution environment against synthetic fixtures
in `demo/`. Two scenarios are provided: one that reaches the approval gate, and
one that fails DNS validation and blocks it.

**Demo mode stops at the approval gate.** No ISO is generated, no virtual media is
attached, no host is powered on. The two playbooks that would do those things
refuse to act while `demo_mode` is true, so the gate can safely be approved.

---

## 1. What the workflow does

```
Bundle intake  ->  Bundle validation  ->  6 parallel readiness checks
                                                    |
                                          Readiness report (always)
                                                    |
                                            Approval gate  [STOP]
                                                    |
                                            Execution (refuses in demo mode)
```

**Approval depends on the report, not on the validation jobs.** The report node
fails deliberately when the verdict is `NOT_READY`, so the approval node's success
edge is never taken. What is approved is the evidence.

Three overall verdicts:

| Verdict | Meaning |
|---|---|
| `READY` | Every branch executed against a real environment and passed |
| `READY_WITH_LIMITATIONS` | Nothing failed, but something was SIMULATED or SKIPPED |
| `NOT_READY` | At least one branch failed. The report job fails; the gate is unreachable |

The passing scenario reports **`READY_WITH_LIMITATIONS`**, because NTP, registry,
VIP and Redfish validation are simulated.

### What each branch actually does in demo mode

| Branch | Behaviour |
|---|---|
| `00-bundle-intake` | Parses the fixture bundle in `demo/bundle/` and derives cluster name, VIPs, CIDRs, MACs, NTP sources, mirror host and six DNS expectations — three fixed records (`api`, `api-int`, `*.apps`) plus one per declared host. Nothing is hardcoded. |
| `01-validate-bundle` | Every semantic assertion runs for real: VIPs inside `machineNetwork`, no CIDR overlap, unique and syntactically valid MACs, `rendezvousIP` belongs to a control-plane host, `rootDeviceHints` present, trust bundle present. |
| `10-validate-dns` | Real grading logic against a fixture zone in `demo/dns/`. This is the branch that differs between the two scenarios. |
| `14-validate-bastion` | Free space measured for real against the execution environment's own filesystem. The client-tool check is simulated, because the fixture bundle carries no `tools/` directory. |
| `11-validate-ntp`, `12-validate-registry`, `13-validate-vips` | Report **SIMULATED** — no NTP server, registry or VIPs exist to contact. |
| `15-validate-bmc` | Reports **SIMULATED** or **SKIPPED** depending on the survey. Never dials out. |

The report labels each branch with its state, so a `READY_WITH_LIMITATIONS`
verdict shows exactly which checks were not executed against a real environment.

---

## 2. Required AAP objects

| Object | Name | Notes |
|---|---|---|
| Organization | `Platform Engineering` | Any name; must match `controller/*.yml` |
| Project | `openshift-airgap-aap` | Points at this Git repository, branch `main` |
| Inventory | `demo` | Source: the project, file `inventories/demo/hosts.yml` |
| Execution environment | `ee-airgap-readiness` | See §4 — **required** |
| Job templates | `00-bundle-intake`, `01-validate-bundle`, `10`–`15`, `20-readiness-report`, `31-generate-agent-iso`, `32-boot-virtual-media` | Created by `controller/job_templates.yml` |
| Workflow | `openshift-site-readiness` | Created by `controller/workflow_templates.yml` |

**No credentials are needed for demo mode.** Everything runs on `localhost` inside
the execution environment. `controller/job_templates.yml` attaches no credentials
when `controller_demo_setup: true`, which is the default.

---

## 3. Setup

1. **Project** — create it pointing at this repository, then sync. Enabling
   *Update revision on launch* means a fixture change is picked up without a
   manual sync.
2. **Inventory** — create `demo`, add a source of type *Sourced from a project*,
   project `openshift-airgap-aap`, file `inventories/demo/hosts.yml`, then sync.
3. **Controller-as-code** — apply from a machine with the `ansible.controller`
   collection and a token:

```bash
export CONTROLLER_HOST=https://aap.apps.example.com
export CONTROLLER_TOKEN=...

ansible-playbook controller/job_templates.yml \
  -e controller_hostname=$CONTROLLER_HOST -e controller_token=$CONTROLLER_TOKEN \
  -e controller_demo_setup=true

ansible-playbook controller/workflow_templates.yml \
  -e controller_hostname=$CONTROLLER_HOST -e controller_token=$CONTROLLER_TOKEN \
  -e controller_demo_setup=true
```

To create the objects by hand instead, use the names in §2 with inventory `demo`,
and enable **Prompt on launch** for inventory on each job template.

---

## 4. Why a custom execution environment is required

Ansible resolves module names at **parse time**, before `when:` is evaluated. The
registry branch skips immediately in demo mode, but its playbook still references
`community.crypto.get_certificate`, so that collection must be present or the job
fails to parse.

`dellemc.openmanage` is **not** required. The one task that uses it lives in
`playbooks/tasks/bmc_dell_bios.yml` and is pulled in with `include_tasks`, which
resolves at run time rather than parse time. The workflow therefore parses and
runs in an execution environment without it, and still uses the certified module
on any environment that carries it.

Minimum contents for demo mode:

| Requirement | Needed by |
|---|---|
| `community.general` | `dig` lookup (production), Redfish modules |
| `ansible.utils` | `ipaddr` / `network_in_network` in `01-validate-bundle` |
| `community.crypto` | Parse-time only in demo mode |
| `netaddr` (pip) | `ansible.utils` filters |

Build it from `execution-environment/`:

```bash
ansible-builder build --container-runtime podman \
  -f execution-environment/execution-environment.yml \
  -t ee-airgap-readiness:1.0 \
  --build-arg PYCMD=/usr/bin/python3.12 \
  --extra-build-cli-args="--platform linux/amd64"
```

Both flags are required:

| Flag | Why |
|---|---|
| `--build-arg PYCMD=/usr/bin/python3.12` | The system-dependency step pulls in RHEL 9's default python3.9, which takes over the `/usr/bin/python3` alternative and has no pip. Without this the build fails with `No module named pip`. |
| `--extra-build-cli-args="--platform linux/amd64"` | Building on Apple silicon otherwise produces an arm64 image an x86_64 cluster cannot run. Drop it on x86_64 hosts. |

The base image requires `podman login registry.redhat.io`; a quay login does not
cover it.

Push the image to your registry and register it in AAP as `ee-airgap-readiness`.

---

## 5. Scenario: `failing_dns`

Launch `openshift-site-readiness` with these survey selections:

| Field | Value |
|---|---|
| Environment | `true` (demo mode) |
| Demo scenario | `failing_dns` |
| BMC validation | `simulated` |
| Approver group | any |
| Bundle URL | leave default — ignored in demo mode |

### Expected results

| Node | Result |
|---|---|
| `00-bundle-intake` | Success. Output shows six DNS assertions derived from the bundle |
| `01-validate-bundle` | Success. Every semantic assertion passes |
| `10-validate-dns` | **Failed.** `api-int` is NXDOMAIN |
| `11`, `12`, `13`, `15` | Success, reporting SIMULATED |
| `14-validate-bastion` | Success |
| `20-readiness-report` | **Failed, deliberately.** Verdict `NOT_READY`. The report is produced and published before the failure, so the evidence exists |
| `approve-execute` | Never becomes reachable, because the report's success edge was not taken |

### Why `api-int` is the record that matters

The fixture omits `api-int` and nothing else: `api` resolves, the wildcard
resolves, and every node record resolves.

This is a realistic failure rather than a contrived one. Someone testing by hand
reaches the cluster through `api` and never queries `api-int`, so the site appears
correct. But `api-int` is what the control plane and the machine-config server
use. Without it the installer reaches bootstrap, waits, and times out roughly
forty minutes later with no obvious cause.

Two things are worth noting in the output:

- **That hostname appears nowhere in this repository.** It is derived from
  `metadata.name` and `baseDomain` in the bundle, which is why adding a site or a
  node requires no code change.
- **The approval node never became reachable.** The readiness report always runs,
  on the success path and the failure path both. When the verdict is `NOT_READY`
  the report job fails deliberately, and the approval node hangs off that job's
  success edge — so the gate is simply never reachable.

Open `20-readiness-report` to see the verdict block: `dns FAIL`, the other
branches labelled with their state, and the failure message naming the exact
record to create.

---

## 6. Scenario: `passing`

Relaunch and change one field:

| Field | Value |
|---|---|
| Demo scenario | `passing` |

### Expected results

| Node | Result |
|---|---|
| `10-validate-dns` | Success — all six records resolve |
| `20-readiness-report` | Success. Verdict `READY_WITH_LIMITATIONS` — nothing failed, but NTP, registry, VIPs and BMC were not executed against a real environment |
| `approve-execute` | Waiting for approval |

The workflow pauses at the approval node, which shows **Approve / Deny**.
Approving it runs `31-generate-agent-iso` and `32-boot-virtual-media`, which
refuse to act and report why:

```
DEMO MODE - nothing generated.
DEMO MODE - no virtual media attached, no host powered on.
```

In a real run those nodes build the agent ISO and attach it over Redfish, powering
on the rendezvous host first. They sit *after* the human decision, not before it.

---

## 7. Resetting between runs

There is nothing to reset and nothing to clean up.

On OpenShift every workflow node runs in its own pod with its own ephemeral local
storage. Nothing a node writes outlives it, and no node reads a file another node
wrote — the durable handoff is workflow artifacts (`set_stats`), not a filesystem.
Each node reconstructs whatever it needs from the repository in demo mode, or from
the bundle URL in production.

To run the failing scenario again, relaunch and select `failing_dns`.

---

## 8. Running without Automation Controller

Both harnesses run from any machine with `ansible-core` and the collections in
`collections/requirements.yml`.

**Node isolation test.** Runs each node as a separate process with its own
filesystem root, passing only workflow artifacts between them — the way Controller
executes a workflow on OpenShift. A node that depends on a sibling's files cannot
pass this test.

```bash
demo/node-isolation-test.sh passing
#   expect: all nodes OK, verdict READY_WITH_LIMITATIONS

demo/node-isolation-test.sh failing_dns
#   expect: 10-validate-dns and 20-readiness-report fail, verdict NOT_READY
```

**Single-process smoke test.** Quicker, less faithful:

```bash
ansible-playbook -i inventories/demo/hosts.yml demo/smoke-test.yml \
  -e demo_scenario=passing        # expect: all plays run, exit 0

ansible-playbook -i inventories/demo/hosts.yml demo/smoke-test.yml \
  -e demo_scenario=failing_dns    # expect: stops at 10-validate-dns
```

`set_stats` values pass between plays only inside Controller, so the smoke test's
report shows every branch as SKIPPED. Use the isolation test to check report
contents.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Job fails to parse, "couldn't resolve module" | The execution environment is missing a collection | §4 — it must carry `community.crypto` even though demo mode skips that branch. It does not need `dellemc.openmanage` |
| `01-validate-bundle` fails on `ipaddr` | `netaddr` missing from the execution environment | Rebuild it; `execution-environment/requirements.txt` includes it |
| Every branch reports SKIPPED in the report | Running the single-process smoke test, or the intake node failed | Use `demo/node-isolation-test.sh`; `set_stats` only flows between workflow nodes |
| Report says `NOT_READY` and the job is red | Correct behaviour when a branch failed | The gate is meant to be unreachable. Fix the environment and relaunch |
| Verdict is `READY_WITH_LIMITATIONS` | Something was SIMULATED or SKIPPED | Expected in demo mode. Approving means accepting those limitations |
| DNS passes when you expected a failure | Scenario left on `passing` | Relaunch with `failing_dns` |
| The approval node never appears | A branch is still failing | Correct behaviour — the gate is unreachable while anything is red |
