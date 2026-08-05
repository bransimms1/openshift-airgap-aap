# Demo-mode runbook — readiness workflow, no hardware required

This is the runbook for demonstrating the readiness workflow on **AAP 2.6 running
on OpenShift, with no bastion, no BMCs, no mirror registry and no lab DNS**.

A fuller version of this demo can be run against real infrastructure -
sushy-emulator for Redfish, CoreDNS serving the cluster zone, a registry with a
private CA - where a lab is available. This runbook needs none of it.

**Runtime:** 10–15 minutes for two runs.
**The demo stops at the approval gate.** No ISO is generated, no virtual media
is attached, no host is powered on. The two playbooks that would do those things
refuse to act while `demo_mode` is true, so approving the gate is safe.

---

## 1. What is demonstrated

```
Bundle intake  ->  Bundle validation  ->  6 parallel readiness checks
                                                    |
                                          Readiness report (always)
                                                    |
                                            Approval gate  [STOP]
                                                    |
                                            Execution (refuses in demo)
```

**Approval depends on the report, not on the validation jobs.** The report node
fails deliberately when the verdict is `NOT_READY`, so the approval node's
success edge is never taken. What a human approves is the evidence.

Three overall verdicts:

| Verdict | Meaning |
|---|---|
| `READY` | Every branch executed against a real environment and passed |
| `READY_WITH_LIMITATIONS` | Nothing failed, but something was SIMULATED or SKIPPED |
| `NOT_READY` | At least one branch failed. Report job fails; gate unreachable |

The demo's passing run reports **`READY_WITH_LIMITATIONS`**, because Redfish,
NTP, registry and VIP validation are simulated. That is the honest answer and
it is worth saying out loud.

Two branches do real work against real inputs:

| Branch | What actually happens in demo mode |
|---|---|
| `00-bundle-intake` | Parses the sample bundle in `demo/bundle/`, derives cluster name, VIPs, CIDRs, MACs, NTP sources, mirror host and six DNS expectations. Nothing is hardcoded. |
| `01-validate-bundle` | Every semantic assertion runs for real — VIPs inside machineNetwork, no CIDR overlap, unique MACs, rendezvousIP belongs to a master, rootDeviceHints present, trust bundle present. |
| `10-validate-dns` | Real grading logic against a fixture zone in `demo/dns/`. This is the branch that fails or passes. |
| `14-validate-bastion` | Free space measured for real against the execution environment. |
| `11`, `12`, `13` | Report **SIMULATED** — no NTP server, registry or VIPs exist to contact. |
| `15-validate-bmc` | Reports **SIMULATED** or **SKIPPED** per the survey. Never dials out. |

Be straight with the audience about which are which. The report labels them, so
they will see it anyway, and saying it first is worth more than hoping nobody
reads the column.

---

## 2. Required AAP objects

| Object | Name | Notes |
|---|---|---|
| Organization | `Platform Engineering` | Any name; must match `controller/*.yml` |
| Project | `openshift-airgap-aap` | Points at this Git repository, branch `main` |
| Inventory | `demo` | Source: the project, file `inventories/demo/hosts.yml` |
| Execution environment | `ee-airgap-readiness` | See §4 — **required** |
| Job templates | `00-bundle-intake`, `01-validate-bundle`, `10`–`15`, `20-readiness-report`, `31`, `32` | Created by `controller/job_templates.yml` |
| Workflow | `openshift-site-readiness` | Created by `controller/workflow_templates.yml` |

**Credentials: none are needed for the demo.** Everything runs on `localhost`
inside the EE. `controller/job_templates.yml` attaches no credentials when
`controller_demo_setup: true`, which is the default.

---

## 3. Setup

```bash
# 1. Project — create it in the UI pointing at this repo, then sync.
#    Settings you want: "Update revision on launch" ON for the demo, so a
#    last-minute fix to a fixture is picked up without a manual sync.

# 2. Inventory — create "demo", add a source of type "Sourced from a project",
#    project openshift-airgap-aap, file inventories/demo/hosts.yml, then sync.

# 3. Apply the controller-as-code (from a machine with the ansible.controller
#    collection and a token):
export CONTROLLER_HOST=https://aap.apps.example.com
export CONTROLLER_TOKEN=...

ansible-playbook controller/job_templates.yml \
  -e controller_hostname=$CONTROLLER_HOST -e controller_token=$CONTROLLER_TOKEN \
  -e controller_demo_setup=true

ansible-playbook controller/workflow_templates.yml \
  -e controller_hostname=$CONTROLLER_HOST -e controller_token=$CONTROLLER_TOKEN \
  -e controller_demo_setup=true
```

If you would rather click: create the job templates by hand with the names in
§2, inventory `demo`, and tick **Prompt on launch** for inventory on each.

---

## 4. Is a custom execution environment actually required?

**Yes.** Not for the reason you would guess.

Ansible resolves module names at parse time, before `when:` is evaluated. Even
though the registry branch skips immediately in demo mode, its playbook still
references `community.crypto.get_certificate`, so that collection must be
present or the job fails to parse.

`dellemc.openmanage` is **not** required. The one task that uses it lives in
`playbooks/tasks/bmc_dell_bios.yml` and is pulled in with `include_tasks`, which
resolves at run time rather than parse time - so the workflow parses and runs in
an EE without it, and still uses the certified module on any EE that has it.

The minimum an EE needs for the demo:

| Requirement | Needed by |
|---|---|
| `community.general` | dig lookup (production), redfish modules |
| `ansible.utils` | `ipaddr` / `network_in_network` in `01-validate-bundle` |
| `community.crypto` | parse-time only in demo |
| `netaddr` (pip) | `ansible.utils` filters — **this was missing and is now in `requirements.txt`** |

Build it from `execution-environment/`:

```bash
ansible-builder build --container-runtime podman \
  -f execution-environment/execution-environment.yml \
  -t ee-airgap-readiness:1.0 \
  --build-arg PYCMD=/usr/bin/python3.12 \
  --extra-build-cli-args="--platform linux/amd64"
```

Both flags are required, and both were found the hard way:

| Flag | Why |
|---|---|
| `--build-arg PYCMD=/usr/bin/python3.12` | The system-dependency step pulls in RHEL9's default python3.9, which takes over the `/usr/bin/python3` alternative and has no pip. Without this the build dies with `No module named pip`. |
| `--extra-build-cli-args="--platform linux/amd64"` | Building on Apple silicon otherwise produces an arm64 image OpenShift cannot run. |

You also need `podman login registry.redhat.io` for the base image - a quay
login does not cover it.

Push it to your registry and register it in AAP as `ee-airgap-readiness`.

> If you genuinely cannot build an EE before the demo, run the smoke test
> (§8) from a laptop and present the terminal output instead. It is a weaker
> demo but it is real, and it beats a job that will not parse.

---

## 5. Run 1 — the failure

Launch `openshift-site-readiness`. Survey selections:

| Field | Value |
|---|---|
| Environment | `true` (lab / demo mode) |
| Demo scenario | **`failing_dns`** |
| BMC validation | `simulated` |
| Approver group | `platform-leads` |
| Bundle URL | leave default — ignored in demo mode |

### Expected results

| Node | Result |
|---|---|
| `00-bundle-intake` | **Success.** Output shows six DNS assertions derived from the bundle. |
| `01-validate-bundle` | **Success.** Every semantic assertion passes. |
| `10-validate-dns` | **FAILED.** `api-int` is NXDOMAIN. |
| `11`, `12`, `13`, `15` | Success, reporting SIMULATED |
| `14-validate-bastion` | Success |
| `20-readiness-report` | **FAILED, deliberately.** Verdict `NOT_READY`. The report is still produced and published first — the evidence exists. |
| `approve-execute` | **Never becomes reachable**, because the report's success edge was not taken. |

### What to say when DNS fails

> "That's the demo. Look at what failed and what didn't — `api` resolves, the
> wildcard resolves, every node record resolves. Only `api-int` is missing.
>
> That's not a contrived failure, it's the one that actually happens. A human
> testing by hand reaches the cluster through `api` and never touches `api-int`,
> so the site looks fine. But `api-int` is what the control plane and the
> machine-config server use. Without it the installer gets to bootstrap, waits,
> and times out about forty minutes in with no obvious cause.
>
> Two things to note. First, that hostname is not written anywhere here — it is derived
> from `metadata.name` and `baseDomain` in the bundle, which is why cluster forty
> gets the same rigour as cluster one. Second, look at the workflow graph: the
> approval node never became reachable. That's not a convention anybody has to
> remember. The readiness report always runs, on the success path and the failure
> path both. When the verdict is NOT_READY the report job fails deliberately, and
> the approval node hangs off that job's success edge - so the gate is simply
> never reachable. A failed run still produces its evidence first."

Open `20-readiness-report` and show the verdict block: `dns FAIL`, the other
branches labelled, and the remediation naming the exact record to create.

---

## 6. Run 2 — the pass and the gate

Relaunch. Change **one** field:

| Field | Value |
|---|---|
| Demo scenario | **`passing`** |

Everything else stays the same.

### Expected results

| Node | Result |
|---|---|
| `10-validate-dns` | **Success** — all six records resolve |
| `20-readiness-report` | **Success.** Verdict **`READY_WITH_LIMITATIONS`** — nothing failed, but NTP, registry, VIPs and BMC were not executed against a real environment |
| `approve-execute` | **Waiting for approval** |

### Approving

The workflow pauses. In the UI the node shows **Approve / Deny**. Click
**Approve**.

`31-generate-agent-iso` and `32-boot-virtual-media` then run and immediately
report:

```
DEMO MODE - nothing generated.
```

Say so plainly:

> "In a real run that node builds the ISO and the next one attaches it over
> Redfish and powers the rendezvous host first. Here they refuse, because
> `demo_mode` is true. The point of showing them is that they sit *after* a
> human decision, not before it."

---

## 7. Resetting between runs

There is nothing to reset, and nothing to clean up.

On OpenShift every workflow node runs in its own pod with its own ephemeral
local storage. Nothing a node writes outlives it, and no node reads a file
another node wrote - the durable handoff between nodes is workflow artifacts
(`set_stats`), not a filesystem. Each node reconstructs whatever it needs from
the repository in demo mode, or from the bundle URL in production.

So there is no shared directory to wipe. To run the failure again, relaunch and
pick `failing_dns`.

---

## 8. Rehearse it — smoke test

Run this **before** the demo, from a machine with `ansible-core` and the
collections in `collections/requirements.yml`:

**Node isolation test — the one that matters.** This runs each node as a
separate process with its own filesystem root and passes only workflow
artifacts between them, which is exactly how Controller executes a workflow on
OpenShift:

```bash
demo/node-isolation-test.sh passing
#   expect: all nodes OK, verdict READY_WITH_LIMITATIONS

demo/node-isolation-test.sh failing_dns
#   expect: 10-validate-dns and 20-readiness-report fail, verdict NOT_READY
```

A node that depends on a sibling's files cannot pass this test. If it is green,
the workflow is safe on OpenShift.

**Single-process smoke test** — quicker, less faithful:

```bash
ansible-playbook -i inventories/demo/hosts.yml demo/smoke-test.yml \
  -e demo_scenario=passing        # expect: all plays run, exit 0

ansible-playbook -i inventories/demo/hosts.yml demo/smoke-test.yml \
  -e demo_scenario=failing_dns    # expect: stops at 10-validate-dns
```

Caveat: `set_stats` values pass between plays only inside Controller, so the
smoke test's report shows every branch SKIPPED. Use the isolation test to
check report contents.

---

## 9. Capture these in advance

Screenshots or saved output, in case the demo environment is unavailable:

1. Workflow graph, run 1, with `10-validate-dns` red and the approval node
   greyed out and unreachable.
2. `10-validate-dns` job output showing the per-record PASS/FAIL list — `api`
   green, `api-int` red.
3. The failure message with the remediation text.
4. `20-readiness-report` job output from run 1 — the verdict block showing
   `dns FAIL` alongside the SIMULATED branches.
5. The rendered HTML report from run 1 (find it under the run's working
   directory on the EE, or take it from a local smoke-test run).
6. Workflow graph, run 2, all green, approval node **Waiting**.
7. The approval dialog itself.
8. Post-approval output showing `DEMO MODE - nothing generated`.

---

## 10. If it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Job fails to parse, "couldn't resolve module" | EE is missing a collection | §4 — the EE must carry `community.crypto` even though the demo skips that branch. It does not need `dellemc.openmanage`. |
| `01-validate-bundle` fails on `ipaddr` | `netaddr` missing from the EE | Rebuild the EE; `requirements.txt` now includes it |
| Everything reports SKIPPED in the report | Running the single-process smoke test, or the intake node failed | Use `demo/node-isolation-test.sh`; `set_stats` only flows between workflow nodes |
| Report says `NOT_READY` and the job is red | Correct behaviour when a branch failed | The gate is meant to be unreachable. Fix the environment and relaunch |
| Verdict is `READY_WITH_LIMITATIONS` | Something was SIMULATED or SKIPPED | Expected in demo mode. Approving means accepting those limitations |
| DNS passes when you wanted it to fail | Scenario left on `passing` | Relaunch with `failing_dns` |
| Approval node never appears | A branch is still failing | That is the correct behaviour — the gate is unreachable while anything is red |
