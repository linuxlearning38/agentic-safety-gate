# AVA v2 Phase 8 End-to-End Release Gate

Date: 2026-05-01
Branch: `v2-development`
Status: Implemented and passed live VirtualBox release gate

## Purpose

Phase 8 proves that `v2.0.0` works as a repeatable system, not a one-off demo.

The release gate connects the full first slice:

- guided AVA provisioning conversation
- approval checkpoint
- one-time temporary credential issuance
- VirtualBox Ubuntu clone creation
- cloud-init access injection
- first-access confirmation
- default hardening choice
- nginx web role bootstrap
- HTTP 200 verification
- verification evidence persistence
- cleanup of the test VM

This is the first test that exercises the v2 product flow from user intent to a
working web server.

## Implemented File

- `tests/v2_e2e_test.py`

## Test Scope

The e2e test covers only the locked v2.0.0 slice:

- provider: `VirtualBox`
- OS: `Ubuntu`
- role: `web_server`
- network: NAT
- firewall profile: `web_public`
- hardening profile: `baseline_linux`

It does not cover:

- AWS, Azure, or GCP
- database server role
- load balancer role
- generic service marketplace
- autonomous healing
- fleet management

## Flow Verified

The test performs this sequence:

1. User asks: `I want a web server in Ubuntu`.
2. AVA routes the request to provisioning.
3. AVA asks for missing specs.
4. Test supplies: `2 CPU, 4 GB RAM, 30 GB disk`.
5. AVA builds desired state and queues approval.
6. Pending approval blocks credential issuance.
7. Test approves the queued request.
8. AVA issues temporary username/password once.
9. Test creates a cloud-init seed using the issued credential.
10. Test clones the Ubuntu cloud-image template in VirtualBox.
11. Test injects cloud-init access media.
12. Test starts the VM.
13. Test confirms SSH reachability and cloud-init marker.
14. Test confirms first login/password-change step in the guided flow.
15. Test accepts default `baseline_linux` hardening.
16. Test bootstraps the `web_server` role.
17. Test verifies host HTTP returns `HTTP 200`.
18. Test runs the Phase 5 verification engine.
19. Test persists verification evidence in SQLite.
20. Test checks total wall time is under 10 minutes.
21. Test destroys the VM during cleanup.

## Rollback Behavior

The e2e test uses `ProvisioningRollbackManager` around the real VM lifecycle.

If failure occurs after a VM is created:

- rollback destroys the partial VM by default
- retain-for-debug can be enabled only by explicit environment flag

This validates that Phase 8 uses the Phase 7 safety primitive instead of leaving
silent broken VMs behind.

## Live Result

Verified on 2026-05-01 with:

- `python tests\v2_e2e_test.py`

Observed result:

- guided flow started and requested missing specs
- approval queued: `5050e177`
- pending approval blocked credential display
- temporary credential issued after approval for user `avaadmin`
- cloud-init seed ISO created
- VM created: `ava-v2-e2e-20260501180536`
- cloud-init access injected
- VM started
- SSH reached through `127.0.0.1:2222`
- cloud-init marker confirmed
- first-login confirmation accepted by guided flow
- hardening choice recorded
- nginx and ufw bootstrapped successfully
- host HTTP returned `HTTP 200`
- verification engine passed
- state store recorded completion
- wall time: `147.6s`
- release gate limit: `600s`
- VM cleanup completed

## Tests Run

During the Phase 8 checkpoint, these were run:

- `python tests\v2_e2e_test.py`
- `python tests\provisioning_phase7_rollback_regression.py`
- `python tests\provisioning_phase6_serving_regression.py`
- `python tests\provisioning_phase5_verification_state_regression.py`
- `python tests\provisioning_phase4_role_bootstrap_regression.py`
- `python tests\provisioning_phase3_policy_credentials_regression.py`
- `python tests\provisioning_phase2_state_regression.py`
- `python -m py_compile tests\v2_e2e_test.py`

All passed.

## Exit Criteria Status

Phase 8 exit criteria:

- `tests/v2_e2e_test.py` passes repeatably: complete for this live run
- total flow completes in under 10 minutes: complete, `147.6s`
- audit/completion evidence is persisted: complete through verification state
  store
- `v2.0.0` eligible for tag only after this phase passes: complete

Phase 8 is closed.

## Boundary

Passing Phase 8 means the v2.0.0 first slice is release-gate ready.

It does not mean:

- every future provider is ready
- every role is ready
- AVA is a multi-cloud platform
- rollback has been tested against every real-world VirtualBox failure

It means the locked v2.0.0 contract has passed its end-to-end release gate.
