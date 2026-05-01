# AVA v2 Phase 7 Failure Modes And Rollback Design

Date: 2026-05-01
Branch: `v2-development`
Status: Implemented as provider-agnostic rollback/reporting primitive

## Purpose

Phase 7 makes the first provisioning slice operationally safe under expected
failures.

The key rule is:

- AVA must not leave a broken partial VM behind silently.

If provisioning fails after a VM has been created, AVA should destroy the
partial VM by default. The user can retain it only through an explicit
retain-for-debug choice.

## Inputs

- Phase 1 VirtualBox adapter
- Phase 4 SSH executor and failure classification
- Phase 5 verification report
- Phase 6 guided serving flow

## Failure Modes Covered

Phase 7 covers these expected failures:

- VirtualBox unavailable
- Ubuntu template/image missing
- VM creation failure
- network configuration failure
- cloud-init/access injection failure
- VM start failure
- SSH timeout or authentication failure
- nginx install failure
- firewall/hardening failure
- service start failure
- verification failure

## Rollback Policy

Default policy:

- if an instance id exists and failure occurs before completion, destroy it
- record rollback status and evidence
- return a clean failure report

Debug policy:

- retain the VM only when the caller explicitly requests retain-for-debug
- record that cleanup was skipped intentionally
- include the retained instance id in the report

No silent retain path is allowed.

## Report Contract

Every failure report must include:

- session id
- phase
- failed step
- failure class
- message
- instance id, when known
- rollback action
- rollback status
- rollback evidence
- timestamp

The report should be safe to show to the user and useful for logs.

## Rollback Actions

Allowed rollback actions:

- `destroy_partial_vm`
- `retain_for_debug`
- `none`

Allowed rollback status values:

- `destroyed`
- `retained`
- `not_needed`
- `failed`

## Implementation Shape

Phase 7 should add:

- `provisioning/rollback.py`
- `tests/provisioning_phase7_rollback_regression.py`

The rollback manager should be provider-agnostic and depend only on the
provider adapter contract.

The VirtualBox adapter already exposes:

- `get_instance_state(instance_id)`
- `destroy_instance(instance_id)`

That is enough for v2.0.0 rollback.

## Implemented Files

- `provisioning/rollback.py`
- `tests/provisioning_phase7_rollback_regression.py`

## Verified Behavior

Regression test:

- `tests/provisioning_phase7_rollback_regression.py`

Verified:

- failure before VM creation needs no cleanup
- default rollback destroys a partial VM when an instance id exists
- rollback action records `destroy_partial_vm`
- failure report preserves failed step and failure class
- already-missing VM cleanup is treated as `not_needed`
- explicit retain-for-debug records `retain_for_debug`
- explicit retain-for-debug does not call destroy
- provider destroy errors are reported as rollback `failed`
- report dictionaries include rollback evidence and timestamps

## Integration Boundary

Phase 7 implementation should not yet turn `/ask` into a full live provisioning
executor.

It should create the safe cleanup/reporting primitive that Phase 8 can use when
the full e2e route runs real VM work.

## Exit Criteria

Phase 7 is complete when regression tests prove:

- failure before VM creation needs no cleanup
- failure after VM creation destroys the partial VM by default
- missing VM cleanup is treated as already clean
- rollback destroy errors are reported cleanly
- retain-for-debug skips destroy only when explicit
- reports contain the expected failure and rollback evidence
