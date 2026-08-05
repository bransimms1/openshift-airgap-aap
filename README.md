# OpenShift Airgap AAP Automation

Ansible Automation Platform content for **disconnected OpenShift bare-metal
deployments**: readiness validation, workflow orchestration with an approval
gate, evidence generation, and Day 2 automation.

It runs as a workflow in Automation Controller on AAP 2.6, and derives every
check from an exported configuration bundle rather than from hardcoded values.

This is a reference project and a starting point. Fork it, point the inventory at
your own site, and remove what you do not need.

## What it does

An agent-based OpenShift installation in a disconnected environment depends on a
list of environmental facts being true before the installer runs: DNS records
resolving, NTP reachable, VIPs unclaimed, the mirror registry serving the release
payload over a trusted chain, and the hardware reachable out of band. When one of
them is wrong, the failure typically surfaces during bootstrap — well after the
mistake was made.

This repository validates those facts first, in parallel, and produces a
readiness report as retained evidence. Execution is gated behind an approval node
that becomes reachable only when validation passes.

| Stage | Playbooks | What it does |
|---|---|---|
| Intake | `00`, `01` | Fetch and digest the bundle, gate on the draft marker, validate the configuration semantically |
| Readiness | `10`–`15` | DNS, NTP, mirror registry, VIPs, bastion, out-of-band hardware — all read-only |
| Evidence | `20` | Readiness report, published as a workflow artifact and rendered as HTML and JSON in the job's working directory |
| Execution | `30`–`32` | Hardware preparation, agent ISO generation, virtual-media boot. All three refuse to act in demo mode |
| Day 2 | `50` | Install Argo CD and hand in-cluster desired state to GitOps |

It does **not** generate cluster configuration, mirror images, or reconcile
in-cluster state. Those belong to
[OpenShift Airgap Architect](https://github.com/bstrauss84/openshift-airgap-architect),
`oc-mirror`, and Argo CD respectively. This repository orchestrates around them.

## Architecture

The bundle is parsed once, by `roles/airgap_bundle_facts`. Everything downstream
consumes what that role publishes through `set_stats`:

```
                       ┌─────────────────────┐
                       │ 00 bundle intake    │  fetch, digest, draft gate
                       └──────────┬──────────┘
                                  │ set_stats: cluster_fqdn, api_vips,
                                  │            expected_dns, expected_macs, …
                       ┌──────────┴──────────┐
                       │ 01 validate bundle  │  semantic checks
                       └──────────┬──────────┘
        ┌───────────┬─────────────┼─────────────┬───────────┐
      10 DNS     11 NTP     12 registry     13 VIPs   14 bastion   15 BMC
        └───────────┴─────────────┼─────────────┴───────────┘
                       ┌──────────┴──────────┐
                       │ 20 readiness report │  runs on success and failure
                       └──────────┬──────────┘
                                  │ succeeds only when the environment is ready
                       ┌──────────┴──────────┐
                       │   approval gate     │
                       └──────────┬──────────┘
                    30 prepare → 31 ISO → 32 boot
```

Two properties are load-bearing:

- **Every workflow node runs in its own pod with its own filesystem.** Nodes do
  not read files written by a sibling; `set_stats` is the handoff. Any node that
  needs the parsed documents includes `airgap_bundle_facts` and re-derives them
  from source.
- **The readiness report fails when the verdict is `NOT_READY`.** That failure is
  what makes the approval gate unreachable, so execution cannot be approved for
  an environment that did not pass.

## Repository layout

```
openshift-airgap-aap/
├── LICENSE                             # MIT
├── ATTRIBUTION.md                      # upstream projects, licenses, trademarks
├── COMPATIBILITY.md                    # supported versions and CI coverage
├── SECURITY.md                         # credential handling and automated checks
├── ansible.cfg
├── .ansible-lint  .yamllint  .gitleaks.toml
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                      # lint, syntax, demo, docs, secrets
│   │   ├── controller-validate.yml     # certified-collection check, manual dispatch
│   │   └── apply-controller-config.yml # applies controller-as-code, manual dispatch
│   └── scripts/
│       ├── secret_scan.py              # literal credentials in credential-shaped keys
│       ├── repo_checks.py              # documentation and fixture consistency
│       ├── test_repo_checks.py         # unit tests for the above
│       └── negative_secret_tests.sh    # secret-scanner detection tests
├── collections/requirements.yml        # runtime collections; public Galaxy only
├── controller/requirements.yml         # ansible.controller, to apply controller/
├── demo/                               # demo fixtures and test harnesses
│   ├── bundle/                         # a synthetic export
│   ├── dns/                            # passing and failing_dns fixture zones
│   ├── smoke-test.yml                  # single-process chain
│   └── node-isolation-test.sh          # each node separately, as AAP runs them
├── docs/
│   ├── DEMO.md                         # run the demo with no hardware
│   ├── DESIGN-NOTES.md                 # maintainer notes
│   └── architecture.svg
├── execution-environment/              # the reproducibility boundary
│   ├── execution-environment.yml
│   └── requirements.yml  requirements.txt  bindep.txt
├── inventories/
│   ├── site-a/                         # a second site is a new inventory
│   └── demo/                           # localhost; used by demo mode
├── playbooks/
│   ├── 00_bundle_intake.yml            # fetch, digest, unpack, draft gate
│   ├── 01_validate_bundle.yml          # semantic validation of the configuration
│   ├── 10_validate_dns.yml             # DNS readiness validation
│   ├── 11_validate_ntp.yml             # NTP source reachability
│   ├── 12_validate_registry.yml        # registry reachability, trust and payload
│   ├── 13_validate_vips.yml            # VIPs must be unclaimed before install
│   ├── 14_validate_bastion.yml         # disk space and client tooling
│   ├── 15_validate_bmc_redfish.yml     # out-of-band hardware validation
│   ├── 20_readiness_report.yml         # runs on success and failure
│   ├── 30_bmc_prepare_hosts.yml
│   ├── 31_generate_agent_iso.yml
│   ├── 32_boot_virtual_media.yml
│   ├── 50_day2_gitops_bootstrap.yml
│   └── 99_remediate_dns.yml            # demo-mode DNS remediation
├── roles/
│   ├── airgap_bundle_facts/            # parses the bundle, derives expectations
│   └── readiness_report/
└── controller/                         # controller-as-code
    ├── credential_types/bmc_redfish.yml
    ├── job_templates.yml
    ├── workflow_templates.yml          # includes the approval node
    ├── surveys/cluster_readiness.json
    └── notifications.yml
```

## Requirements

| Component | Version |
|---|---|
| Ansible Automation Platform | 2.6 |
| ansible-core | 2.15.13 or later — see `COMPATIBILITY.md` |
| Python | 3.11 |
| OpenShift | agent-based installer |

Runtime collections resolve from public Galaxy. `ansible.controller` is Automation
Hub content and is needed only to apply the controller-as-code.

## Quick start

```bash
# Runtime collections - everything imported by playbooks/ and roles/.
ansible-galaxy collection install -r collections/requirements.yml

# Build the execution environment. Both flags are required:
#   PYCMD          system dependencies pull in RHEL9's python3.9, which takes
#                  over /usr/bin/python3 and has no pip; pinning the interpreter
#                  keeps the build on the 3.12 that does.
#   --platform     building on Apple silicon otherwise produces an arm64 image
#                  an x86_64 cluster cannot run. Drop it on x86_64 hosts.
ansible-builder build --container-runtime podman \
  -f execution-environment/execution-environment.yml \
  -t ee-airgap-readiness:1.0 \
  --build-arg PYCMD=/usr/bin/python3.12 \
  --extra-build-cli-args="--platform linux/amd64"

# Applying the controller-as-code needs a separate dependency. ansible.controller
# is certified content from Automation Hub and is not installed by the line above.
ansible-galaxy collection install -r controller/requirements.yml

# Run the whole readiness chain locally against the demo fixtures. Each node
# runs as a separate process carrying artifacts forward, the way Automation
# Controller executes the workflow.
demo/node-isolation-test.sh passing
```

### Running a readiness playbook on its own

The readiness playbooks consume facts published by `00_bundle_intake.yml`
through `set_stats`. **Workflow artifacts do not cross `ansible-playbook`
process boundaries**, so running two playbooks as two separate local commands
does not carry `cluster_fqdn`, `expected_dns` or anything else between them.

Three supported ways to run them:

| Method | Use when |
|---|---|
| The `openshift-site-readiness` workflow in Automation Controller | Production. Controller passes artifacts between nodes |
| `demo/node-isolation-test.sh <scenario>` | Locally, for the full chain. Reproduces Controller's artifact passing |
| A single `ansible-playbook` run with the required variables supplied | Locally, for one branch |

For the third, supply the facts the branch needs as extra vars. For
`10_validate_dns`:

```bash
ansible-playbook -i inventories/site-a/hosts.yml playbooks/10_validate_dns.yml \
  -e cluster_fqdn=ocp.example.com \
  -e '{"expected_dns": [{"name": "api.ocp.example.com", "expect": ["10.42.10.5"], "why": "API VIP"}]}' \
  -e '{"dns_servers": ["10.42.10.53"]}'
```

`roles/airgap_bundle_facts/tasks/main.yml` lists everything published; the
readiness branch you are running names what it reads. Alternatively, include the
role at the top of your own play so the facts are derived in the same process.

### In Automation Controller

Apply `controller/` with the `ansible.controller` collection and launch the
`openshift-site-readiness` workflow. See **Controller-as-code prerequisites**
below — the definitions expect several objects to exist already.

## Controller-as-code prerequisites

`controller/` defines job templates, the workflow, the survey, a custom
credential type and notifications. It does **not** create the objects those
definitions reference. Create these first, with these names, or override the
variables shown.

| Prerequisite | Default name | Override |
|---|---|---|
| Organization | `Platform Engineering` | `-e controller_organization=...` |
| Project, Git-backed, pointing at this repository | `openshift-airgap-aap` | `-e controller_project=...` |
| Inventory — demo | `demo`, sourced from the project, file `inventories/demo/hosts.yml` | `-e controller_inventory_demo=...` |
| Inventory — production | `site-a`, sourced from the project or your own source | `-e controller_inventory_prod=...` |
| Execution environment | `ee-airgap-readiness` | `-e controller_execution_environment=...` |
| Credential to apply this with | A *Red Hat Ansible Automation Platform* credential pointing at the same controller | see below |
| `ansible.controller` collection | `ansible-galaxy collection install -r controller/requirements.yml` | — |

Production mode additionally expects these credentials to exist, because the job
templates attach them by name: `bastion-ssh`, `mirror-registry`, `demo-bmc`
(the BMC credential, created from the custom type in
`controller/credential_types/`), and `ocp-pull-secret`.

```bash
# Demo mode - the current default. No credentials are attached, so this works
# on a fresh controller with no site secrets in existence yet.
ansible-playbook controller/credential_types/bmc_redfish.yml
ansible-playbook controller/job_templates.yml      -e controller_demo_setup=true
ansible-playbook controller/workflow_templates.yml -e controller_demo_setup=true

# Production - attaches the credentials named above and defaults to site-a.
ansible-playbook controller/job_templates.yml      -e controller_demo_setup=false
ansible-playbook controller/workflow_templates.yml -e controller_demo_setup=false
```

`controller_demo_setup` defaults to **`true`**. Pass `false` explicitly for a
real site.

The most reliable way to apply this against AAP 2.6 is from inside AAP itself:
create one job template by hand running `controller/job_templates.yml` with the
AAP credential attached, and run it. `ansible.controller` then reads the host and
token from the environment and no token is passed as an extra variable. Applying
from a workstation instead needs `-e controller_hostname=... -e controller_token=...`.

## Demo mode

Demo mode runs the readiness workflow with **no bastion, no BMCs, no mirror
registry and no lab DNS**, using synthetic fixtures under `demo/`. Two scenarios
are provided: one that reaches the approval gate, and one that fails DNS
validation and blocks it.

```bash
demo/node-isolation-test.sh passing
demo/node-isolation-test.sh failing_dns
```

The harness runs each workflow node as a separate process with its own filesystem
root, carrying artifacts forward the way Automation Controller does on OpenShift.
See `docs/DEMO.md` for the full runbook.

The playbooks that generate installation media or power on hardware refuse to act
while `demo_mode` is true.

## Production considerations

- **Credentials come from Automation Controller**, never from the repository or a
  bundle. BMC credentials use the custom credential type in
  `controller/credential_types/`; every task that touches them sets `no_log: true`.
- **Pin and sign the execution environment.** Record its image digest in the
  readiness report — the template already carries the field.
- **Decide where evidence is retained.** The structured `readiness_report`
  artifact is retained by Automation Controller with the workflow job, and is
  what downstream nodes and the audit trail consume. The HTML and JSON files
  that `20_readiness_report` renders are written inside the execution pod and
  are **ephemeral** — this repository does not upload them to object storage, a
  PVC or an artifact repository. If you need durable rendered reports, add that
  step; nothing here provides it.
- **Use automation mesh rather than SSH across network boundaries.** Place
  execution nodes inside the isolated segment and reach them through hop nodes.
- **Do not run the AAP that builds a cluster on that cluster.** The dependency
  only becomes visible on the day you need to rebuild.
- **Adopt read-only first.** Readiness validation (`10`–`15`) needs no change
  control. Bundle intake and the gate follow, then the hardware read path, then
  execution and Day 2.

## Compatibility

See `COMPATIBILITY.md` for tested ansible-core versions, collection floors, and
what CI does and does not exercise.

## Security

See `SECURITY.md` for credential handling, demo-fixture caveats, and the
automated checks that run in CI.

## License

MIT — see `LICENSE`. See `ATTRIBUTION.md` for upstream projects, third-party
collection licenses and trademarks. This is a personal project and is not an
official Red Hat product.
