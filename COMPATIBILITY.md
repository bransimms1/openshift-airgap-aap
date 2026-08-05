# Compatibility

## Support matrix

| Component | Target or tested version |
|---|---|
| Ansible Automation Platform | 2.6 |
| `ansible.controller` | >= 4.6.0 |
| ansible-core | 2.15.13, 2.16.14, and current release |
| Python | 3.11 |
| OpenShift | agent-based installer |
| `oc-mirror` | v2 (`mirror.openshift.io/v2alpha1`) |

`2.16.14` matches the execution environment shipped with AAP 2.6 and is the
version lint runs against. `2.15.13` is the minimum supported. The current
release is tested unpinned, so that a core regression is visible before it
reaches a supported version.

Nothing below ansible-core 2.15 is tested.

### Collection versions

Collections are declared with floors, not pins, in `collections/requirements.yml`
and `execution-environment/requirements.yml`:

| Collection | Floor | Used for |
|---|---|---|
| `ansible.utils` | >= 5.1.2 | network and IP address filters |
| `ansible.posix` | >= 1.6.0 | callbacks, general POSIX modules |
| `community.general` | >= 9.0.0 | `redfish_info`, `redfish_command`, `dig` lookup |
| `community.crypto` | >= 2.20.0 | `get_certificate`, `x509_certificate_info` |
| `kubernetes.core` | >= 5.0.0 | Day 2 |

CI resolves the newest release satisfying each floor, so a breaking collection
release surfaces as a CI failure. For a disconnected site the opposite is
correct: pin exact versions, sync them into Private Automation Hub, sign, and
verify.

`ansible.controller` is Automation Hub content, is declared separately in
`controller/requirements.yml`, and is needed only to apply the controller-as-code.

### Known limitations

- Current `community.crypto` and `community.general` releases no longer declare
  support for ansible-core below 2.17, so the 2.15 and 2.16 matrix entries emit a
  `Collection ... does not support Ansible version` warning. These are warnings,
  not failures, and both entries pass. AAP's own execution environment ships
  collection versions matched to its core.
- On AAP 2.5 and later the platform gateway does not serve `/api/v2/`, so
  `awx.awx` cannot drive a supported AAP install. Use `ansible.controller`, which
  is present in the supported execution environment.
- AAP 2.6 is the last release installable from RPM (RHEL 9 only). 2.7 onward is
  containerized-installer only.
- `dellemc.openmanage` is optional and is not declared in any requirements file.
  See `ATTRIBUTION.md`.

## CI coverage

| Covered | Where |
|---|---|
| Every playbook parses, on all three ansible-core versions | syntax job |
| Both demo scenarios run end to end, each node in its own process and filesystem root | demo job |
| A failed validation makes the approval gate unreachable | demo job, `failing_dns` |
| Controller-as-code parses, via `awx.awx` namespace substitution | syntax job |
| No literal credentials in the tree or in any commit | secrets job |
| Documentation references, fixtures and build instructions match the tree | docs job |
| yamllint and ansible-lint, production profile | lint job |

## Not covered by CI

| Not covered | Why |
|---|---|
| Run-time behaviour of `redfish_info`, `redfish_command`, `get_certificate`, `x509_certificate_info` and the `dig` lookup | Demo mode reaches none of them — there is no BMC and no registry in CI, so those branches report `SIMULATED`. CI proves the tasks parse and the modules resolve, not that they work against real endpoints |
| Whether `ansible.controller` accepts these module parameters | Automation Hub content, which a public runner cannot install. `awx.awx` shares module names but the two collections drift. `controller-validate.yml` checks this and requires `AUTOMATION_HUB_TOKEN`; without it the job is skipped rather than passed |
| Applying controller-as-code to a live Controller | Mutates a real system. Belongs in an opt-in job against a disposable instance |
| Building the execution environment image | Requires `registry.redhat.io` credentials |
| Behaviour against real hardware | There is none in CI, which is why demo fixtures exist |
| Whether `demo/bundle/` would install | The fixtures are intentionally not installable — see `SECURITY.md` |
