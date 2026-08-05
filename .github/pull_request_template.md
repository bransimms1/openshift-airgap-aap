## What changed

<!-- One line. -->

## Which stage does this affect?

- [ ] Readiness validation (read-only — no change control needed to run)
- [ ] Bundle intake / gate
- [ ] Hardware (BMC, firmware, BIOS) — **changes hardware state**
- [ ] Execution (ISO, virtual media) — **boots machines**
- [ ] Day 2
- [ ] Controller configuration

## Checklist

- [ ] No credentials, pull secrets, certificates or real site hostnames added
- [ ] New checks derive their expectations from the bundle, not from hardcoded values
- [ ] Any task touching credentials sets `no_log: true`
- [ ] Failure messages name the remediation, not just the failure
- [ ] Tested against a real Airgap Architect export bundle
