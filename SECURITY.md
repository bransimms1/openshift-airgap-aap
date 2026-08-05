# Security

## Credential handling

**Never commit credentials to this repository.** Credentials belong in
Automation Controller and are injected at run time.

- BMC credentials come from the custom credential type in
  `controller/credential_types/bmc_redfish.yml`. Every task that touches them
  sets `no_log: true`, so they do not appear in job output.
- The pull secret comes from its own Controller credential and is never read
  from a bundle or from the tracked tree.
- In playbooks and inventories, reference credentials as variables
  (`{{ bmc_password }}`), never as literal values.
- `.gitignore` excludes real `install-config.yaml`, `agent-config.yaml` and
  `imageset-config.yaml` exports, along with certificates, keys, pull secrets and
  vault passwords, so they cannot be committed by accident.

For a disconnected site, also pin and sign the execution environment and record
its image digest in the readiness report.

## Demo fixtures

Fixtures under `demo/bundle/` describe a cluster that does not exist. Hostnames,
addresses and identifiers are synthetic, and they contain no credentials.

**They are intentionally not installable.** `additionalTrustBundle` holds a
non-empty sentinel value, and `pullSecret` and `sshKey` are absent rather than
empty. The fixtures exist to exercise the parsing and validation logic; they
prove nothing about whether `openshift-install` would accept them.

## Automated checks

CI runs the following on every push and pull request:

| Check | What it covers |
|---|---|
| `secret_scan.py` | Literal values under credential-shaped keys. Accepts references — `{{ var }}`, `${VAR}`, `${{ secrets.X }}` — and rejects values that mix a reference with a literal fragment |
| gitleaks | General-purpose secret detection over the working tree and the full commit history, using the default rule set |
| `negative_secret_tests.sh` | Negative tests for the repository's secret-scanning rules: injects credential-shaped material into throwaway copies and asserts each scanner reports it, with the expected rule and location |
| `repo_checks.py` | Documentation links and references resolve, demo fixtures are tracked, fixture MAC addresses are valid, fixtures carry nothing credential-shaped |

There is no `.gitleaksignore` and no gitleaks allowlist, and CI asserts that both
remain absent. If a finding is genuinely a false positive, prefer, in order:
change the content so it no longer looks like a credential; add a
`.gitleaksignore` fingerprint, which pins a single finding in one file at one
commit and expires when the line changes; or, as a last resort, an allowlist
entry with `condition = "AND"`, an anchored path regex and a `regexTarget` —
never a bare path, which exempts the whole file.

## Reporting a vulnerability

This is a personal project with no support commitment and no warranty.

If you find a security problem, open a GitHub issue describing the impact and how
to reproduce it. If you would rather not report it publicly, contact the
maintainer through their GitHub profile.
