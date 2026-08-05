# Attribution

This project is licensed under the MIT License (see `LICENSE`). This file records
what it integrates with and what it depends on.

## OpenShift Airgap Architect

This repository is built to run **around**
[OpenShift Airgap Architect](https://github.com/bstrauss84/openshift-airgap-architect).

The integration is by file format. This automation reads the export that tool
produces — `install-config.yaml`, `agent-config.yaml`, `imageset-config.yaml`, and
the `DRAFT_NOT_VALIDATED.txt` marker — and derives its validation expectations
from those documents. It integrates with the documented output format and
includes no code from that project.

Consult that project for its own license and terms. Nothing here relicenses it or
implies endorsement by it or its authors.

## Upstream formats and behaviour

The validation logic depends on documented behaviour of the following. No code
from any of them is included here.

| Depends on | For what |
|---|---|
| OpenShift agent-based installer | The schema of `install-config.yaml` and `agent-config.yaml`, and the absence of an out-of-band management driver — which is why hardware preparation lives outside the installer |
| `oc-mirror` v2 | The `mirror.openshift.io/v2alpha1` ImageSetConfiguration format, and that v2 emits `IDMS`/`ITMS` rather than `ImageContentSourcePolicy` |
| Kubernetes / OpenShift API | Node, `BareMetalHost` and `MachineConfigPool` objects, used as an inventory source and as maintenance safety interlocks |
| DMTF Redfish | Out-of-band hardware inventory and power/boot control |
| Automation Controller | Workflow semantics: `set_stats` as the artifact handoff between nodes, approval nodes, and `all_parents_must_converge` |

## Third-party Ansible collections

Collections are runtime dependencies resolved at install time. None is vendored
here and none is relicensed by this project. Each retains its own license and
terms — refer to its repository or Automation Hub listing.

**Required.** Declared in `collections/requirements.yml` and
`execution-environment/requirements.yml`:

- `ansible.utils`, `ansible.posix`, `community.general`, `community.crypto`,
  `kubernetes.core` — community collections, each under its own license

**Required only to apply controller-as-code.** Declared in
`controller/requirements.yml`:

- `ansible.controller` — Red Hat certified content, distributed through
  Automation Hub under its own terms

### Optional integrations

**`dellemc.openmanage`** is an optional integration and is not declared in any
requirements file. `playbooks/tasks/bmc_dell_bios.yml` uses it, and
`playbooks/15_validate_bmc_redfish.yml` reaches that file with `include_tasks`,
which resolves at run time and only when `bmc_vendor == 'dell'`. Every playbook
therefore parses, and the readiness workflow runs, on an execution environment
that does not carry the collection.

It is left undeclared because it is Automation Hub content — declaring it would
require Hub credentials during project sync and in public CI for a collection the
automation may never load — and because its `bindep` requirements (`isomd5sum`,
`syslinux`, `xorriso`) are not available in UBI9-minimal's repositories. Add it
where you have Dell hardware and an execution environment built on a fuller base.
It is Red Hat certified content, distributed through Automation Hub under its own
terms.

`redhat.openshift` is likewise not declared: nothing here imports it.

## Base container image

`execution-environment/execution-environment.yml` builds on
`registry.redhat.io/ansible-automation-platform-26/ee-minimal-rhel9`. That image
is Red Hat content, subject to Red Hat's terms, and requires authentication to
pull. It is referenced, not redistributed.

## Authorship

Widely used Ansible, OpenShift, Kubernetes, Redfish and Automation Controller
patterns are not claimed as original. Deriving expectations from configuration,
`serial: 1` for rolling node maintenance, honouring PodDisruptionBudgets on
drain, and approval gates in a workflow graph are established practice.

## Trademarks

Red Hat, OpenShift, Ansible and Ansible Automation Platform are trademarks of
Red Hat, Inc. Dell, iDRAC and OpenManage are trademarks of Dell Inc. HPE and iLO
are trademarks of Hewlett Packard Enterprise. Use here is descriptive and does
not imply affiliation with or endorsement by any of them.

**This is a personal project. It is not an official Red Hat product, is not
supported by Red Hat, and carries no warranty. See `LICENSE`.**
