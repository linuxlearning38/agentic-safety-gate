# AVA v2.0.0 Phase Plan

Date: 2026-04-29
Status: Execution plan for `v2.0.0`
Scope lock: VirtualBox + Ubuntu + `web_server` only

## Purpose

This document defines how `v2.0.0` gets built.

It is subordinate to:

- `docs/AVA_V2_ARCHITECTURE.md`

If the architecture doc defines **what** `v2.0.0` is, this file defines
**how** it will be executed.

Anything outside these phases is not part of `v2.0.0`.

---

## Delivery Rule

`v2.0.0` is complete only when:

- the guided provisioning flow works end to end
- `tests/v2_e2e_test.py` passes
- nginx returns HTTP 200 on the provisioned VM
- AVA produces a complete audit trail and completion report
- total wall-clock flow stays under 10 minutes

---

## Scope Lock

This plan covers only:

- provider: `VirtualBox`
- OS: `Ubuntu`
- role: `web_server`
- conversation flow: guided, stateful, approval-aware
- post-provision continuation: first-login confirmation, hardening, verification

This plan does **not** cover:

- AWS, Azure, GCP
- second VM role
- generic service installer
- healing loops
- multi-VM orchestration
- fleet state management

---

## Phase 0 — Branch And Contract Setup

### Goal

Start v2 in a controlled place with the contract already locked.

### Input

- `docs/AVA_V2_ARCHITECTURE.md`
- current `master` state after `v1.0.4` and `v1.1` polish

### Work

- create branch: `v2-development`
- confirm `master` remains frozen except critical bugs
- confirm architecture doc is the contract
- confirm this phase plan is the execution guide

### Output

- isolated `v2-development` branch
- v2 contract and phase plan both committed

### Exit Criteria

- branch exists
- architecture doc is present on branch
- phase doc is present on branch
- no implementation starts before this is done

### Estimate

- 30 minutes

---

## Phase 1 — Provider Foundation

### Goal

Create the minimal VirtualBox provider adapter that can manage one Ubuntu VM lifecycle.

### Input

- locked provider contract from architecture doc
- existing experimental provisioning branch only as reference, not authority

### Work

- create `provisioning/adapters/base.py`
- create `provisioning/adapters/virtualbox.py`
- implement:
  - `plan_instance`
  - `create_instance`
  - `start_instance`
  - `stop_instance`
  - `destroy_instance`
  - `get_instance_state`
  - `get_connection_info`
- use Ubuntu cloud image / template strategy, not ISO install
- ensure adapter output is structured and auditable

### Output

- working VirtualBox adapter module
- local unit tests with mocked `VBoxManage`

### Exit Criteria

- adapter can create and destroy a test VM locally
- provider state can be queried reliably
- no role/bootstrap logic is mixed into adapter code

### Estimate

- 2 to 3 working days

---

## Phase 2 — Session State And Desired State

Status: implemented as a standalone regression checkpoint on 2026-05-01.

### Goal

Create the stateful guided workflow foundation before role bootstrap begins.

### Input

- provisioning intent shape from architecture doc
- locked desired-state fields

### Work

- create `provisioning/desired_state.py`
- create `provisioning/conversation/session_manager.py`
- create `provisioning/conversation/flow_engine.py`
- create SQLite-backed session persistence
- implement session phases:
  - `intent_detected`
  - `awaiting_vm_type`
  - `awaiting_specs`
  - `awaiting_approval`
  - `provisioning`
  - `awaiting_first_login`
  - `awaiting_post_login_choices`
  - `bootstrapping`
  - `verifying`
  - `completed`
  - `failed`
  - `cancelled`
- validate desired-state fields and defaults

### Output

- persistent session model
- desired-state dataclass and validator
- resume/cancel support for provisioning conversations

### Implemented Files

- `provisioning/desired_state.py`
- `provisioning/conversation/session_manager.py`
- `provisioning/conversation/flow_engine.py`
- `tests/provisioning_phase2_state_regression.py`

### Verified Behavior

- guided web-server requests move to `awaiting_specs`
- missing `cpu`, `ram_gb`, and `disk_gb` are requested deterministically
- completed specs produce a validated desired state
- desired state defaults remain locked to VirtualBox, Ubuntu, `web_server`,
  NAT, `web_public`, and `baseline_linux`
- sessions survive SQLite manager reload
- cancel moves a session to a terminal state
- unsupported roles are rejected before provider execution

### Exit Criteria

- browser refresh or process restart does not lose provisioning session state
- AVA can move deterministically from one phase to the next
- invalid desired-state combinations are rejected before provider execution

### Estimate

- 2 working days

---

## Phase 3 — Policy, Approval, And Credential Flow

Status: implemented as a standalone regression checkpoint on 2026-05-01. See
`docs/AVA_V2_PHASE3_POLICY_CREDENTIALS.md`.

### Goal

Wire provisioning into AVA’s policy and approval model while defining the temporary access workflow.

### Input

- desired-state model
- existing approval and OPA model from v1

### Work

- create `provisioning/policy.py`
- create `provisioning/credentials.py`
- enforce provisioning approval for `v2.0.0`
- implement one-time temporary username/password generation
- define first-login continuation checkpoint:
  - user confirms first login and password change in AVA
- ensure credential display is one-time and not casually reprinted

### Output

- approval-aware provisioning gate
- temporary credential workflow
- deterministic continuation from approval to provisioning and from login confirmation to post-login actions

### Exit Criteria

- AVA cannot provision without approval
- temp credentials can be issued and consumed by the flow
- AVA can pause at `awaiting_first_login` and resume after user confirmation

### Estimate

- 1 to 2 working days

---

## Phase 4 — Web Server Role And Bootstrap

Status: implemented after live VirtualBox web bootstrap smoke on 2026-05-01. See
`docs/AVA_V2_PHASE4_DESIGN.md`.

### Goal

Build the first full role: `web_server`.

### Input

- running VM from provider adapter
- reachable session and desired-state flow

### Work

- use `docs/AVA_V2_PHASE4_DESIGN.md` as the Phase 4 implementation contract
- create `provisioning/roles/base.py`
- create `provisioning/roles/web_server.py`
- create `provisioning/bootstrap/ssh_executor.py`
- implement role-defined bootstrap only:
  - nginx installation
  - service enable/start
  - role-specific firewall behavior
- implement hardening behavior through `baseline_linux`
- default hardening on
- opt-out confirmation path if user explicitly declines

### Output

- role contract
- working web server bootstrap
- hardening-capable post-login continuation

### Implemented Files

- `provisioning/roles/base.py`
- `provisioning/roles/web_server.py`
- `provisioning/bootstrap/ssh_executor.py`
- `tests/provisioning_phase4_role_bootstrap_regression.py`
- `tests/virtualbox_web_server_bootstrap_smoke.py`

### Verified Behavior

- `web_server` role is locked to `nginx`, `ufw`, `ssh`, `22/tcp`, and `80/tcp`
- SSH executor returns structured command evidence
- command failures are classified into the Phase 4 failure vocabulary
- live clone accepts cloud-init access
- live SSH bootstrap installs nginx and ufw
- UFW allows SSH before enablement
- UFW allows HTTP before nginx verification
- nginx is enabled and active
- guest-local HTTP check passes
- host NAT HTTP returns `HTTP 200`
- smoke VM is destroyed after verification

### Exit Criteria

- AVA can bootstrap nginx onto the created Ubuntu VM
- AVA can apply default baseline hardening without breaking the role
- no generic installer behavior exists

### Estimate

- 2 to 3 working days

---

## Phase 5 — Verification And State Recording

Status: implemented after Phase 5 regression and live VirtualBox web smoke on 2026-05-01.

### Goal

Make AVA prove that it succeeded and persist the resulting lifecycle state.

### Input

- provisioned VM
- bootstrapped nginx role

### Work

- create `provisioning/verify/engine.py`
- create `provisioning/state/store.py`
- verify:
  - VM exists
  - VM is running
  - connection path works
  - nginx is active
  - expected port is open
  - HTTP 200 is returned
- persist:
  - session outcome
  - desired state
  - actual state
  - verification result
  - timestamps

### Output

- verification engine
- provisioning state store
- evidence-backed completion report

### Implemented Files

- `provisioning/verify/engine.py`
- `provisioning/state/store.py`
- `tests/provisioning_phase5_verification_state_regression.py`
- `tests/virtualbox_web_server_bootstrap_smoke.py`

### Verified Behavior

- verification requires VM existence, running state, connection metadata, SSH reachability, nginx activity, guest HTTP, and host HTTP
- every verification check records status, timestamp, evidence, and failure class where applicable
- stopped or unreachable VMs fail cleanly before role-level checks
- host HTTP failure produces a failed report instead of a success assumption
- successful verification persists as a `completed` provisioning record
- failed verification persists as a `failed` provisioning record
- desired state, actual state, verification evidence, and timestamps are stored in SQLite
- live VirtualBox web smoke now saves verification evidence after nginx returns `HTTP 200`

### Exit Criteria

- AVA never reports success without verification evidence
- failed verification results in a clean failure state
- completion report includes timestamp and evidence

### Estimate

- 1 to 2 working days

---

## Phase 6 — AVA Integration

Status: implemented as guided `/ask` serving integration on 2026-05-01. See
`docs/AVA_V2_PHASE6_SERVING_INTEGRATION.md`.

### Goal

Connect the provisioning modules to the actual AVA serving contract and guided conversation experience.

### Input

- provider adapter
- session engine
- desired-state validator
- approval flow
- role bootstrap
- verification

### Work

- integrate provisioning intent into AVA routing
- connect guided question flow to `/ask`
- ensure AVA can:
  - ask VM type
  - ask missing specs
  - request approval
  - continue after approval
  - continue after first-login confirmation
  - offer hardening
  - report completion
- ensure AVA language stays operator-facing and coherent

### Output

- user-facing guided provisioning flow inside AVA

### Implemented Files

- `provisioning/serving.py`
- `control/input_router.py`
- `web_agent_v2.1_guardrail.py`
- `tests/provisioning_phase6_serving_regression.py`

### Verified Behavior

- provisioning requests such as `I want a web server in Ubuntu` route to the v2 guided flow
- provisioning diagrams remain on the architecture/diagram path instead of starting a VM session
- active provisioning sessions accept spec answers without requiring the user to repeat the original request
- AVA queues approval once CPU, RAM, and disk specs are collected
- AVA does not issue temporary credentials while approval is pending
- approved sessions issue temporary credentials once and move to first-login confirmation
- first-login confirmation moves the session to post-login hardening choices
- hardening choice is recorded and moves the session to the bootstrapping checkpoint
- unrelated knowledge prompts are not hijacked when no provisioning session is active

### Exit Criteria

- a user can drive the whole `web_server` flow from the AVA interface
- AVA can resume the conversation at the correct phase
- no freeform shell shortcut bypasses the provisioning contract

### Estimate

- 2 working days

---

## Phase 7 — Failure Modes And Rollback

### Goal

Make the first slice operationally safe under expected failures.

### Input

- integrated provisioning flow

### Work

- define and implement failure handling for:
  - VirtualBox unavailable
  - image/template missing
  - VM creation failure
  - network configuration failure
  - SSH timeout
  - nginx install failure
  - hardening failure
  - verification failure
- default behavior:
  - destroy partial VM
- optional behavior:
  - retain VM for debugging when user explicitly chooses it

### Output

- rollback and failure-handling behavior
- predictable error reporting path

### Exit Criteria

- partial failures do not leave silent broken VMs behind by default
- retain-for-debug path is explicit, not accidental
- AVA returns clean failure reports with actionable next steps

### Estimate

- 1 to 2 working days

---

## Phase 8 — End-to-End Test And Release Gate

### Goal

Prove that `v2.0.0` works as a repeatable system, not a one-off demo.

### Input

- fully integrated guided provisioning flow

### Work

- create `tests/v2_e2e_test.py`
- automate full flow:
  - start guided provisioning
  - supply role/spec answers
  - approval checkpoint
  - provision VM
  - confirm login/password change
  - apply default hardening
  - verify nginx
  - assert HTTP 200
  - assert completion report and audit evidence
- measure total wall time

### Output

- repeatable `v2.0.0` end-to-end test
- release gate for tagging

### Exit Criteria

- `tests/v2_e2e_test.py` passes repeatably
- total flow completes in under 10 minutes
- audit log and completion report are complete
- `v2.0.0` is eligible for tag only after this phase passes

### Estimate

- 1 to 2 working days

---

## Suggested Sequence Summary

1. Phase 0 — branch and contract setup
2. Phase 1 — provider foundation
3. Phase 2 — session state and desired state
4. Phase 3 — policy, approval, credentials
5. Phase 4 — role and bootstrap
6. Phase 5 — verification and state
7. Phase 6 — AVA integration
8. Phase 7 — failure modes and rollback
9. Phase 8 — end-to-end release gate

---

## Rules For Using This Plan

- finish phases in order
- do not add second-provider work inside first-provider phases
- do not add second-role work inside first-role phases
- if a phase expands unexpectedly, stop and review scope before continuing
- if code does not clearly map to one phase, it probably does not belong in `v2.0.0`
