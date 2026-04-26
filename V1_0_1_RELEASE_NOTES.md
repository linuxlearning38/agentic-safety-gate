# AVA v1.0.1 Release Notes

Date: April 26, 2026  
Status: Stable patch release for AVA v1 master scope

## Summary

v1.0.1 is a hardening and validation patch on top of v1.0.  
No scope expansion was introduced.

Master remains focused on:

- Grounded DevOps knowledge responses.
- Safe read-only operational checks.
- Approval-gated medium-risk actions.
- Deterministic blocking of destructive actions.

Provisioning work remains isolated on:

- `provisioning-v0.1-experimental`

## What Changed

### 1. Security hardening fixes

- Closed a destructive-request bypass for punctuation variants (for example `truncate my database?`).
- Preserved destructive blocking behavior while keeping learning prompts informational.

### 2. Routing correctness fixes

- Ensured state-changing actions like `install security updates` and `stop process 1234` route to approval-gated flows.
- Prevented definition/self prompts from being misrouted into operational inspection paths.
- Improved ambiguous service phrasing handling so placeholders like `my` are not treated as literal service names.

### 3. Permanent rigorous test harness

- Added `tests/ava_500_robust_audit.py` as a repeatable live robustness suite.
- Added this audit to `AGENTS.md` verification baseline.

## Validation Snapshot

Live and regression verification for v1.0.1:

- `tests/ava_live_100_question_test.py`: 100/100 PASS
- `tests/ava_500_robust_audit.py`: 500/500 PASS
- Full master test sweep (`tests/*.py`, excluding runner): 13/13 PASS
- 429 retries during 500 robust audit: 0
- Service health: `GET /health` = `ok`

Path integrity checks:

- Path 1 fix commits are present on master.
- Path 2 split remains intact (provisioning absent on master, preserved on `provisioning-v0.1-experimental`).

## Compatibility

- No breaking API change intended within v1 scope.
- No migration required.

