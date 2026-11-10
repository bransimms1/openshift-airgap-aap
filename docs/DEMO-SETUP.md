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

Launch `controller-as-code`. It should finish with `ok=15 failed=0`. It creates:

- the `BMC / Redfish` credential type
- twelve job templates, `00-bundle-intake` through `32-boot-virtual-media`, on
  inventory `demo` with EE `ee-airgap-readiness` and no credentials attached
- the `openshift-site-readiness` workflow with its five-question survey, its 13
  nodes and the approval gate

It is safe to run again; a second run reports `changed=0`.

The approval gate is created in two tasks: one creates the approval node, the
next links its success edge to `prepare-hosts`. Passing `success_nodes`
together with `approval_node` makes the module post the edge to the approval
template as well, which AAP rejects. CI checks that the two stay separate.

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
fallback, and save both dashboards beforehand (§5) so they open instantly.

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
- Switch to the dashboard for this run (§5). Point at the red verdict, the one
  failed card, the `NXDOMAIN` row in the DNS table, and *Execution blocked*.
  The job that produced it failed, and the evidence still survived it.

**5:00 – Passing run (3 min).** Relaunch with *Demo scenario* `passing`. DNS
passes, and the report shows `READY_WITH_LIMITATIONS`: nothing failed, but it
lists exactly which checks were simulated or skipped. Open this run's
dashboard next to the visualizer: the tally shows **1 passed, 4 simulated, 1
skipped**, and the approval panel lists the limitations an approver accepts.
It says *eligible*, not *approved*: the report is written before anyone
decides. Back in AAP the workflow stops at **Authorise execution**. Read the
description, then click **Approve**.

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

## 5. Viewing the readiness dashboard

`20-readiness-report` renders a self-contained HTML dashboard: the verdict,
one card per check, the approval status, and every piece of evidence the
branches published. The job's pod, and the files in it, are deleted when the
job ends, so the role also publishes the page, compressed, as the
`readiness_report_html` job artifact. Controller keeps job artifacts in its
database, and `tools/fetch_readiness_report.py` turns one back into a file.

It needs Python 3.8 or later and nothing else, so the `python3` that ships
with macOS works.

### Find the job

In AAP, open **Automation Execution → Jobs**, then the
`openshift-site-readiness` run. In its visualizer, click the
**20-readiness-report** node. The job ID is in that job's details and in the
page URL. The number on the workflow run itself is the *workflow job* ID.

You need only one of them:

```bash
cd openshift-airgap-aap
export AAP_HOST=https://aap.apps.example.com
export AAP_TOKEN=...            # see "Credentials" below

tools/fetch_readiness_report.py 1234 --open             # the report job ID
tools/fetch_readiness_report.py --workflow 1230 --open  # or the workflow job ID
tools/fetch_readiness_report.py --latest --open         # or the newest report job
```

The helper prints the path of the saved file, for example
`job1234-readiness-report-ocp-demo-1762817867.html` in the current directory,
along with the verdict. `--open` opens the file in your default browser. Without
it, run `open job1234-readiness-report-*.html`. Add `-o ~/Desktop/` to save it
somewhere else, and `--json` to save the structured report next to it.

### Both scenarios

Retrieve one dashboard per run. The failing run's report job ends **Failed**,
deliberately, and its dashboard is retrieved the same way. The role publishes
the artifact before it fails the job.

```bash
tools/fetch_readiness_report.py --workflow <failing_dns workflow job ID> -o ~/Desktop/
tools/fetch_readiness_report.py --workflow <passing workflow job ID>     -o ~/Desktop/
```

### Credentials

- **Token:** create one in AAP under **Access Management → Users → your user
  → Tokens**, with *Read* scope, and put it in `AAP_TOKEN`.
- **Username and password:** export `AAP_USERNAME` instead and the helper
  prompts for the password. `AAP_PASSWORD` is read too, if it is set.

The helper sends the credential only to `AAP_HOST`, never prints it, and never
writes it into the page. It needs read access to the job and nothing more.
Saved files are created readable only by you (mode `0600`).

If the AAP route uses the cluster's self-signed ingress certificate, Python
rejects it. Pass the cluster's CA:

```bash
oc get configmap default-ingress-cert -n openshift-config-managed \
  -o jsonpath='{.data.ca-bundle\.crt}' > ingress-ca.pem
tools/fetch_readiness_report.py 1234 --ca-file ingress-ca.pem --open
```

`--insecure` skips verification instead. Use it only on a lab cluster. With a
python.org install of Python on macOS, run its *Install Certificates* command
once, or public CAs will fail too.

If you cannot run the helper against AAP, save the job's JSON another way and
decode it offline:
`curl -H "Authorization: Bearer $AAP_TOKEN" "$AAP_HOST/api/controller/v2/jobs/1234/" > job.json`,
then `tools/fetch_readiness_report.py --from-file job.json`.

### What survives the pod, and for how long

| Item | Where it lives | Lifetime |
|---|---|---|
| `readiness_report`, the structured result set | Job artifact, Controller database | Until the job is deleted |
| `readiness_report_html`, the dashboard (gzip, base64, about 10 KB) | Job artifact, Controller database | Until the job is deleted |
| HTML and JSON files in `/tmp/readiness-report-<job id>/` | The job's pod | Gone when the job ends |

Jobs are deleted by AAP's **Cleanup Job Details** management job, on whatever
schedule your AAP has. Nothing here changes that schedule. A deleted job
returns `404`, and the helper says so. Save the HTML with the change record
if you need it for longer. The page shows hostnames and addresses: share it
the way you would share the job output.

Both artifacts are also passed to the approval node and the execution jobs as
extra variables, the way every `set_stats` artifact is passed down a
workflow. If a site's report ever compresses to more than
`report_html_artifact_max_bytes` (256 KB by default), the role publishes a
note in place of the page. The helper reports that, and `--json` still saves
the structured report.

### Do I need to rebuild anything?

- **No EE rebuild.** The role uses `community.general.archive` and
  `ansible.builtin.slurp`, which `ee-airgap-readiness` already carries. The
  helper runs on your workstation, not in the EE.
- **No controller-as-code rerun.** The templates, the workflow and the survey
  are unchanged.
- **Sync the project.** *Update revision on launch* does this when the workflow
  is next launched. Jobs that ran before the update published no dashboard.
  The helper says so, and `--json` saves their structured report.

### Presenting: visualizer and dashboard side by side

1. Before the session, open the dashboards saved for the `failing_dns` and
   `passing` runs, in browser tabs next to the AAP visualizer tabs for those
   runs.
2. **Failing run.** In the visualizer, click the red `20-readiness-report`
   node and show the verdict block in its output. Switch to the dashboard: the
   same verdict, the DNS card in red, the `api-int` row in the evidence table,
   and *Execution blocked*. Go back to the visualizer and show that the
   approval node never ran.
3. **Passing run.** On the dashboard, show the tally and the limitations
   list, then *Eligible for human approval*. In the visualizer, open
   **Authorise execution** and approve it there. The dashboard does not change
   when you approve: it records the evidence, not the decision.
4. For anyone who asks where the page came from, run the helper live against
   the run you just finished.

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| Project sync fails at *Fetch galaxy collections* | The organization has a Galaxy credential and AAP cannot reach Galaxy. Remove the credential from the organization; the EE already has the collections |
| Job pods stuck pulling the image (`ImagePullBackOff`) | Missing `system:image-puller` grant (§2), or a wrong image path in the EE |
| `couldn't resolve module/action 'community.crypto...'` | The job template is not using `ee-airgap-readiness` |
| `controller-as-code` fails with `Unsupported parameters ... parents` | An old checkout. Sync the project |
| `controller-as-code` fails at `Node - approval gate` with `could not be found at .../workflow_approval_templates/<id>/success_nodes/` | An old checkout, from before the gate's edge moved to its own task. Sync the project and rerun |
| Templates exist but there is no workflow | The bootstrap ran `controller/job_templates.yml`. Point it at `controller/apply_all.yml` |
| `controller-as-code` fails with `... not found` | Objects 1–4 are missing, or are named differently. Create them or pass the matching `controller_*` variables |
| Inventory `demo` has no hosts | Sync its project source |
| `bastion` shows `SKIPPED` | Expected in an EE pod; see the table at the top |
| The approval node never appears | Expected for `failing_dns`. For `passing`, check that every branch reported |
| `fetch_readiness_report.py`: `HTTP 404 ... /api/controller/v2/jobs/<id>/` | Wrong ID, a workflow job ID (use `--workflow`), or the job was cleaned up |
| `fetch_readiness_report.py`: `published the structured readiness_report but no dashboard` | The job ran before the project was synced. Relaunch the workflow, or use `--json` |
| `fetch_readiness_report.py`: `TLS verification failed` | Pass the ingress CA with `--ca-file` (§5) |
