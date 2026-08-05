# Design notes

Maintainer notes for the automation in this repository. Read before changing the
validation roles or the controller definitions.

## Technical facts this automation depends on

These are documented behaviours of the upstream OpenShift Airgap Architect project
and the agent-based installer. Several checks exist because of them, so changing
those checks without reading this section risks automation that appears correct and
validates nothing.

- **`agent-config.yaml` has no `bmc:` block.** The agent-based installer has no
  out-of-band management driver at all. That is the whole reason hardware preparation
  lives outside the installer, in `30_bmc_prepare_hosts.yml`. (Bare-metal IPI *does*
  accept `bmc:` in `install-config.yaml` — do not carry assumptions between them.)
- **`DRAFT_NOT_VALIDATED.txt`** is written into an export when the generating tool had
  unresolved warnings. `roles/airgap_bundle_facts` turns its presence into a hard gate
  rather than a warning, which is deliberate.
- **Imageset output is `mirror.openshift.io/v2alpha1`** — oc-mirror v2, producing
  IDMS/ITMS, not the older `ImageContentSourcePolicy`. Checks that look for ICSP will
  silently pass against nothing.
- **Bundle contents:** `install-config`, `agent-config`, `imageset-config`,
  `FIELD_MANUAL.md`, `99-chrony-ntp-{master,worker}.yaml`, and an optional `tools/`
  carrying `oc`, `oc-mirror`, `openshift-install[-fips]` and the mirror-registry
  tarball.
- **AAP 2.6 is the last release installable from RPM**, and RHEL 9 only. From 2.7 the
  containerized installer is the only supported path — worth planning for before a
  long-lived deployment is stood up.

## Design decisions worth preserving

- **Checks derive their expectations from the configuration; no playbook contains a
  hostname.** This is the scaling property of the whole repository — a new site is a
  new inventory, not new code. Resist hardcoding, even temporarily.
- **`all_parents_must_converge: true` on the readiness report node** makes it wait
  for every validation branch, and its parent edges are `always`, so the report runs
  on the failure path too. The approval node does *not* use converge: it has a single
  `success` edge from the report. The report fails deliberately when the verdict is
  `NOT_READY`, which is what makes the gate unreachable. Approval therefore means a
  human accepted the evidence, not that some jobs finished. All three parts are
  load-bearing.

- **Every workflow node is independent.** On AAP running on OpenShift each node
  executes in its own pod: no shared filesystem, and `set_fact` values do not survive
  the node that created them. Nodes that need the parsed bundle include
  `airgap_bundle_facts` and reconstruct it from source; nodes that need derived values
  read them from workflow artifacts. Nothing reads a file another node wrote. Preserve
  this — it is why a node can be relaunched on its own.
- **Read-only first.** The recommended adoption order deliberately puts the install
  last: readiness (weeks 1–2) → configuration-derived checks (weeks 3–4) → hardware
  baseline (month 2) → execution and Day 2 (month 3+).
- **`no_log: true` on every task touching credentials.** Credentials come from AAP
  credential types, never from the repository or a bundle.
- **Firmware drift and certificate expiry are currently `ignore_errors: true`** —
  findings rather than blockers. Flip them to hard asserts if local policy demands it.

## Known open items

- `set_stats` promotes facts into downstream workflow nodes **only inside Automation
  Controller**. Running the playbooks by hand in one `ansible-playbook` process gives
  you execution coverage but not artifact passing, so the readiness report will show
  every branch as `SKIPPED`. `demo/node-isolation-test.sh` reproduces Controller's
  behaviour faithfully — separate process per node, artifacts carried forward — and is
  the harness to trust when changing anything about the handoff.
- `01_validate_bundle.yml` uses `ansible.utils.network_in_network` for CIDR overlap.
  Verify against real bundles — IPv6 and dual-stack semantics are fussier than IPv4,
  and the upstream project supports IPv6-only single-stack.

## Demo mode

`docs/DEMO.md` explains how to run the readiness workflow with no bastion, no BMCs and
no registry.

`demo/` is a runtime dependency of demo mode, not optional decoration:
`roles/airgap_bundle_facts` copies `demo/bundle/` into the working directory when
`demo_mode` is true, and `playbooks/10_validate_dns.yml` resolves against
`demo/dns/<scenario>.yml`. Deleting the directory breaks demo mode. Production mode
(`demo_mode: false`) never reads it — that is the part that is optional.

Note the `.gitignore` exception for `demo/bundle/*.yaml`: the repository-wide rules
exclude `install-config.yaml`, `agent-config.yaml` and `imageset-config.yaml` so real
exports are never committed, and without the negation the fixtures would be silently
dropped from a clone.
