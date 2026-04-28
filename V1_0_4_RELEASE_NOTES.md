# AVA v1.0.4 Release Notes

Date: 2026-04-28
Status: Runtime quality patch

## What's New in v1.0.4

`v1.0.4` is a focused quality release built on `v1.0.3`.

This patch improves the live serving contract, vulnerability reporting,
architecture/diagram routing, and validation rigor without expanding AVA's
product scope.

---

### Runtime Vulnerability Scanning

- Restored runtime vulnerability scanning after the earlier Trivy cache/path failure
- Scanner failures now return controlled, concise operator-facing results
- Vulnerability output now explains:
  - how many findings are shown out of the total
  - how many unique CVE IDs are involved
  - how many findings have a reported fix version versus `no fix available`

### Host Risk And Security Output

- `what should I investigate on this host` now stays aligned with the actual primary concern
- Host-risk planning no longer drifts into unrelated follow-up steps when the completed CVE scan is already the main issue
- Suspicious-activity output no longer treats read-only host systemd limitations as real failed-service alerts

### Knowledge And Diagram Quality

- Improved `blue-green vs canary deployment` so the answer is clean, specific, and operator-usable
- Restored readable Mermaid diagram presentation with normal color behavior
- Fixed `docker architecture diagram` so it stays on the controlled architecture path and no longer falls into an approval flow

### Serving Contract Hardening

- Added stronger normalization for wrapped or polite prompt variants
- Hardened the serving contract against casing, punctuation, and phrasing mutations
- Preserved deterministic behavior for approvals, destructive blocking, clarifications, diagrams, and knowledge paths

---

## Validation Snapshot

Current validation on `v1.0.4` candidate:

| Check | Result |
|---|---|
| `GET /health` | `status=ok` |
| `tests/intelligence_regression.py` | PASS |
| `tests/host_observability_regression.py` | PASS |
| `tests/vulnerability_scan_timeout_regression.py` | PASS |

Rigorous live validation completed on `2026-04-28`:

| Suite | Result |
|---|---|
| `tests/ava_500_rigorous_live_audit.py --rounds 5 --start-round 0 --delay 0.35` | 500/500 PASS |
| `tests/ava_500_rigorous_live_audit.py --rounds 5 --start-round 5 --delay 0.35` | 500/500 PASS |
| `tests/serving_contract_regression.py` | PASS |

Reference:
- `RIGOROUS_VALIDATION_2026-04-28.md`

---

## Compatibility

- No breaking changes from `v1.0.3`
- No scope expansion beyond the v1 master contract
- Same Docker deployment model
- Same approval and destructive-blocking boundaries

---

## Scope Reminder

`master` remains intentionally scoped to:

- grounded DevOps answers
- safe operational inspection
- approval-aware guarded actions
- deterministic destructive blocking

Still not included on `master`:

- Linux VM provisioning execution
- multi-provider orchestration
- unattended hardening or healing automation

---

## Roadmap (Unchanged Direction)

- `v1.1`: more public-polish and operator UX cleanup
- `v2.0`: provisioning foundation with one provider, one role, one full lifecycle
- `v3.0`: guarded healing and broader lifecycle automation
