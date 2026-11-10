# Demo setup: AAP 2.6 on OpenShift

Set up and present the `openshift-site-readiness` workflow in about 10 minutes,
using an existing AAP 2.6 on OpenShift. You need no bare metal, bastion, mirror
registry, Redfish endpoint or lab DNS. Everything runs inside the execution
environment against fixtures in `demo/`.

For what each node does in detail, see [DEMO.md](DEMO.md).

---

## What is real and what is simulated

Say this out loud during the demo. It is the difference between a demo and a
claim.

| Node | In this demo |
|---|---|
| `00-bundle-intake` | **Real logic, sample input.** Parses `demo/bundle/` and derives the cluster name, VIPs, MACs, NTP sources and the six DNS records that must exist |
| `01-validate-bundle` | **Real.** Every semantic check runs against the sample bundle |
| `10-validate-dns` | **Real grading, fixture zone.** Records are resolved against `demo/dns/<scenario>.yml`, not a DNS server |
| `11-validate-ntp`, `12-validate-registry`, `13-validate-vips`, `15-validate-bmc` | **SIMULATED.** Nothing is contacted. The report labels them that way |
| `14-validate-bastion` | **SKIPPED** in an EE pod. The pod's root filesystem is overlay, which Ansible's mount facts leave out. On a real bastion it measures free space |
| `20-readiness-report` | **Real.** Grades the branches and fails on `NOT_READY` |
| `30`, `31`, `32` (behind the gate) | **Refuse to act.** Each prints `DEMO MODE - ...` and stops |

### Why the demo cannot touch real infrastructure

- The `demo` inventory has one host, `localhost` (the job's own pod), and an
  empty `bmc` group. There is nothing else to connect to.
- `demo_mode: true` is set in `inventories/demo/group_vars/all/demo.yml`, and the
  survey's default is `true`. Every playbook that could change anything (`30`,
  `31`, `32`) checks it first and stops.
- Demo mode attaches no credentials to any job template, so no BMC, registry
  or SSH secret exists for a job to use.
- If someone picks `Environment = false` against the `demo` inventory, intake
  tries to download from the placeholder `artifacts.example.com` and fails.
  Nothing downstream runs.

---

## 1. Prerequisites

| You need | Notes |
|---|---|
| AAP 2.6 on OpenShift, with an admin login | Any AAP 2.6 that runs jobs in container groups |
| AAP able to reach this Git repository | Public GitHub works. Otherwise use a fork or internal mirror |
| A Linux or macOS build host with `podman`, Python 3, `oc` | On Windows, use WSL |
| A Red Hat login for `registry.redhat.io` | For the EE base image |
| A registry AAP can pull from | The OpenShift internal registry (below), or Quay or similar |

---

## 2. Build and publish the execution environment

The EE must contain `community.general`, `community.crypto`, `ansible.utils`,
`ansible.posix` and `netaddr`. `12-validate-registry` references
`community.crypto` at parse time even though demo mode skips that branch.
`execution-environment/` already declares all of it, with collection versions
pinned to releases that support the ansible-core in AAP 2.6.

```bash
git clone https://github.com/bransimms1/openshift-airgap-aap.git
cd openshift-airgap-aap

podman login registry.redhat.io
python3 -m pip install 'ansible-builder>=3.1,<4'

ansible-builder build --container-runtime podman \
  -f execution-environment/execution-environment.yml \
  -t ee-airgap-readiness:1.0 \
  --build-arg PYCMD=/usr/bin/python3.12
# On Apple silicon add:  --extra-build-cli-args="--platform linux/amd64"

# Check what went in.
podman run --rm ee-airgap-readiness:1.0 ansible --version
podman run --rm ee-airgap-readiness:1.0 ansible-galaxy collection list
```

`--build-arg PYCMD=/usr/bin/python3.12` is required. [DEMO.md §3](DEMO.md)
explains why.

### Push to the OpenShift internal registry

```bash
# Once per cluster (cluster-admin): expose the internal registry.
oc patch configs.imageregistry.operator.openshift.io/cluster \
  --type merge -p '{"spec":{"defaultRoute":true}}'

REG=$(oc get route default-route -n openshift-image-registry -o jsonpath='{.spec.host}')
oc new-project aap-ee        # or any namespace you own

podman login -u "$(oc whoami)" -p "$(oc whoami -t)" "$REG"   # add --tls-verify=false for a self-signed ingress cert
podman tag  ee-airgap-readiness:1.0 "$REG/aap-ee/ee-airgap-readiness:1.0"
podman push "$REG/aap-ee/ee-airgap-readiness:1.0"

# Let job pods in the AAP namespace pull it. Replace "aap" with your AAP namespace.
oc policy add-role-to-group system:image-puller system:serviceaccounts:aap -n aap-ee
```

In AAP the image is then
`image-registry.openshift-image-registry.svc:5000/aap-ee/ee-airgap-readiness:1.0`.

To use Quay or another registry instead, push there and attach a *Container
Registry* credential to the EE in AAP.

---

## 3. Configure AAP

Create these objects once, by hand. The names match what `controller/` expects.

| # | Object | Settings |
|---|---|---|
| 1 | **Organization** `Platform Engineering` | Leave *Galaxy credentials* empty. The EE already carries the pinned collections, so project sync then skips the Galaxy download and needs no internet access |
| 2 | **Execution environment** `ee-airgap-readiness` | Image from §2. Pull: *Always*. Organization: `Platform Engineering` |
| 3 | **Project** `openshift-airgap-aap` | Git, the repository URL, branch `main`, *Update revision on launch* on. Sync it and wait for *Successful* |
| 4 | **Inventory** `demo` | Add a source: *Sourced from a Project*, project `openshift-airgap-aap`, file `inventories/demo/hosts.yml`, *Overwrite* on. Sync it. Expect one host, `localhost`, and `demo_mode: true` in the inventory variables |
| 5 | **Credential** `aap-bootstrap` | Type *Red Hat Ansible Automation Platform*, the AAP URL, an admin user and password |
| 6 | **Job template** `controller-as-code` | Inventory `demo`, project `openshift-airgap-aap`, playbook `controller/apply_all.yml`, EE *Default execution environment* (it must contain `ansible.controller`; the shipped `ee-supported-rhel9` does), credential `aap-bootstrap`, extra variables `controller_demo_setup: true` |

Launch `controller-as-code`. It should finish with `ok=14 failed=0`. It creates:

- the `BMC / Redfish` credential type
- twelve job templates, `00-bundle-intake` through `32-boot-virtual-media`, on
  inventory `demo` with EE `ee-airgap-readiness` and no credentials attached
- the `openshift-site-readiness` workflow with its five-question survey, its 13
  nodes and the approval gate

It is safe to run again; the second run only refreshes the approval node.

To use an existing organization instead, add `controller_organization: <name>`
to the extra variables and create objects 2–4 in that organization.

**From a workstation instead of job template 6** (needs `ansible.controller` from
Automation Hub: `ansible-galaxy collection install -r controller/requirements.yml`):

```bash
ansible-playbook controller/apply_all.yml -e controller_demo_setup=true \
  -e controller_hostname=https://aap.apps.example.com -e controller_token="$TOKEN"
```

### Rehearse once

Before presenting, launch the workflow once with `failing_dns` and once with
`passing` (§4). This pulls the EE image onto the nodes, so the live run is not
waiting on the pull. It also tells you how long a run takes on your cluster;
pod start-up dominates. Keep both finished runs open in browser tabs as a
fallback.

---

## 4. The 10-minute walkthrough

Open **Templates → `openshift-site-readiness` → Workflow visualizer** before you
start.

**0:00 – The problem (1 min).** A disconnected OpenShift install depends on many
facts being true: DNS, NTP, free VIPs, a trusted mirror, reachable hardware.
When one is wrong, you find out about forty minutes into bootstrap. This
workflow checks them first and puts a human approval in front of anything that
changes hardware.

**1:00 – The graph (1.5 min).** Walk the visualizer left to right:
- intake and bundle validation
- six read-only checks in parallel
- the readiness report, which waits for all six (*converge*) and runs on
  success or failure (*always* edges)
- the approval gate, on the report's **success** edge only
- three execution nodes behind it

Mention that the whole graph was created from `controller/` by one job
template.

**2:30 – Failing run (2.5 min).** Launch. The survey asks only five things; the
cluster name, VIPs and DNS records are not among them because they come from
the bundle. Choose *Environment* `true`, *Demo scenario* `failing_dns`, and leave
the rest. While it runs:
- `00-bundle-intake` output shows the derived cluster facts and six DNS
  records. No hostname is hardcoded in the repository.
- `10-validate-dns` turns red: `api-int` does not resolve, with a remediation
  message naming the exact record.
- Say plainly that `11`, `12`, `13` and `15` report **SIMULATED**: nothing real
  was contacted.
- `20-readiness-report` is red, by design: `NOT_READY`, with `dns FAIL
  api-int...`. The approval node never ran. Without the evidence, nobody is
  even asked.

**5:00 – Passing run (3 min).** Relaunch with *Demo scenario* `passing`. DNS
passes, and the report shows `READY_WITH_LIMITATIONS`: nothing failed, but it
lists exactly which checks were simulated or skipped. The workflow stops at
**Authorise execution**. Open it and read the description: approving means
accepting those limitations. Click **Approve**.

**8:00 – Behind the gate (1 min).** `30`, `31` and `32` run, and each prints
`DEMO MODE - no hardware touched`, `nothing generated`, or `no virtual media
attached`. In production these power off hosts, build the agent ISO and boot it
over Redfish. They run only after a human has approved the evidence.

**9:00 – Wrap (1 min).** Production uses the same graph with a site inventory,
`controller_demo_setup=false` (credentials attached) and the real bundle URL.
The simulated branches then run against real resolvers, NTP sources, the
mirror registry and BMCs. Adopt it read-only first: checks `10`–`15` change
nothing.

---

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| Project sync fails at *Fetch galaxy collections* | The organization has a Galaxy credential and AAP cannot reach Galaxy. Remove the credential from the organization; the EE already has the collections |
| Job pods stuck pulling the image (`ImagePullBackOff`) | Missing `system:image-puller` grant (§2), or a wrong image path in the EE |
| `couldn't resolve module/action 'community.crypto...'` | The job template is not using `ee-airgap-readiness` |
| `controller-as-code` fails with `Unsupported parameters ... parents` | An old checkout. Sync the project |
| Templates exist but there is no workflow | The bootstrap ran `controller/job_templates.yml`. Point it at `controller/apply_all.yml` |
| `controller-as-code` fails with `... not found` | Objects 1–4 are missing, or are named differently. Create them or pass the matching `controller_*` variables |
| Inventory `demo` has no hosts | Sync its project source |
| `bastion` shows `SKIPPED` | Expected in an EE pod; see the table at the top |
| The approval node never appears | Expected for `failing_dns`. For `passing`, check that every branch reported |
