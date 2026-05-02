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

Status: implemented as provider-agnostic rollback/reporting primitive on
2026-05-01. See `docs/AVA_V2_PHASE7_DESIGN.md`.

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

### Implemented Files

- `provisioning/rollback.py`
- `tests/provisioning_phase7_rollback_regression.py`

### Verified Behavior

- failure before VM creation needs no cleanup
- failure after VM creation destroys the partial VM by default
- missing VM cleanup is treated as already clean
- rollback destroy errors are reported cleanly
- retain-for-debug skips destroy only when explicit
- reports contain failure and rollback evidence

### Exit Criteria

- partial failures do not leave silent broken VMs behind by default
- retain-for-debug path is explicit, not accidental
- AVA returns clean failure reports with actionable next steps

### Estimate

- 1 to 2 working days

---

## Phase 8 — End-to-End Test And Release Gate

Status: Reopened on 2026-05-02 — module-level e2e passed, but live
chat-to-VM path is incomplete. See `docs/AVA_V2_PHASE8_E2E_RELEASE_GATE.md`.

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

### Implemented Files

- `tests/v2_e2e_test.py`

### Verified Behavior

- guided provisioning starts from `I want a web server in Ubuntu`
- AVA collects CPU, RAM, and disk specs
- approval is queued before infrastructure changes
- pending approval does not expose credentials
- approved session issues temporary credentials once
- VirtualBox clone is created from the Ubuntu cloud-image template
- cloud-init access media is injected
- first-access marker is confirmed over SSH
- first-login confirmation and hardening choice are accepted by the guided flow
- nginx and ufw are bootstrapped on the guest
- host NAT HTTP returns `HTTP 200`
- verification engine passes
- completion evidence is persisted in SQLite
- wall time is under the 10-minute release gate
- test VM is destroyed during cleanup

### Exit Criteria

| Criterion | Status |
| --- | --- |
| `tests/v2_e2e_test.py` passes repeatably | Met at module level (Windows native, direct module calls) |
| Total flow completes in under 10 minutes | Met at module level (`147.6s`) |
| Audit log and completion report are complete | Met at module level |
| Guided provisioning flow works end to end via live chat | **NOT MET** — chat path stops before VM creation |
| `v2.0.0` eligible for tag | **Blocked** — requires Phase 9 exit criteria first |

### Estimate

- 1 to 2 working days

### Why Phase 8 Was Reopened

Reopened on 2026-05-02 after live validation against the running AVA chat service.

- Phase 8's e2e test (`tests/v2_e2e_test.py`) is a module-integration test that runs Python
  natively on Windows and calls `VBoxManage` directly. It bypasses the `/ask` chat path entirely.
- The actual user-facing flow goes through `/ask` in AVA chat, which runs inside the Docker
  container. The container cannot reach `VBoxManage` on the Windows host.
- Live validation on 2026-05-02 confirmed: chat collects specs, gates approval, issues
  credentials, records first-login confirmation, and records the hardening choice — but never
  creates a VM.
- Therefore the Phase 8 exit criterion "guided provisioning flow works end to end" is met at
  module level but **NOT** at user-facing chat level.
- Phase 9 (runner bridge) must close this gap before `v2.0.0` can tag.

---

## Phase 9 — Host-Side Runner Bridge

### Goal

Close the gap between AVA chat (in Docker) and VirtualBox (on Windows host) so that approval
through chat triggers actual VM creation, bootstrap, hardening, verification, and result
reporting back to the chat session.

### Input

- Phase 6 serving integration (chat path through `/ask`)
- Phase 7 rollback manager (provider-agnostic)
- Phase 8 e2e module integration (proves the modules work)
- Existing Redis instance (already in `docker-compose`)
- Existing host-side `VBoxManage` path (already proven by `tests/v2_e2e_test.py`)

### Work

- create `provisioning/runner/job_queue.py` (Redis-backed approved-job queue)
- create `provisioning/runner/host_runner.py` (Windows-side worker process)
- create `provisioning/runner/result_writer.py` (writes results back to AVA state store)
- update `provisioning/serving.py` to enqueue jobs after approval
- update `provisioning/serving.py` to read job status for verify/evidence/status prompts
- create `scripts/start_host_runner.ps1` (start the worker on Windows)
- create `tests/provisioning_phase9_runner_bridge_regression.py`
- create `tests/v2_chat_to_vm_e2e_test.py` (the real user-flow e2e: drive through HTTP `/ask`,
  approve, wait for runner, verify HTTP 200)

### Job Queue Contract

Redis keys and message format:

- queue key: `ava:provisioning:jobs:approved`
- result key prefix: `ava:provisioning:jobs:result:<job_id>`
- status key prefix: `ava:provisioning:jobs:status:<job_id>`
- job message: `{ job_id, session_id, desired_state, credentials_seed_data, enqueued_at, expires_at }`
- status values: `queued`, `picked_up`, `provisioning`, `bootstrapping`, `hardening`,
  `verifying`, `completed`, `failed`, `cancelled`
- result message: `{ job_id, instance_id, instance_name, ssh_host, ssh_port, http_port,
  verification_evidence, completion_timestamp, error (if failed) }`

### Credential Handling Contract

- `credentials_seed_data` may contain a short-lived temporary password only long enough for
  the host runner to build the cloud-init seed.
- Redis result messages must never contain the temporary password.
- Runner logs must never print temporary passwords or rendered cloud-init user-data.
- Local cloud-init seed files containing secrets must be deleted after the VM starts unless
  retain-debug is explicitly enabled.
- If secret cleanup fails, the job must be marked `failed` with a clear warning; AVA must not
  hide possible secret residue.
- AVA status/evidence prompts report credential state as issued yes/no only.

### Host Runner Contract

- runs as a long-lived Python process on Windows host (NOT in Docker)
- polls Redis for approved jobs (`BLPOP` with timeout)
- picks up one job at a time (single-worker for v2.0.0; multi-worker is v2.1+)
- executes the same module sequence that `tests/v2_e2e_test.py` executes today
- writes status updates to Redis after each phase
- writes final result to Redis on completion or failure
- handles its own crash recovery: if it restarts, it does NOT re-pick a job already marked
  `picked_up` (job becomes orphaned and requires manual cleanup for v2.0.0)
- logs every action to a host-side log file

### Failure Handling

- runner crash mid-job: orphaned job, manual cleanup required for v2.0.0
- `VBoxManage` failure: rollback via Phase 7 manager, write `failed` result, next job picked
  up normally
- Redis connection lost: runner exits cleanly, requires manual restart
- Docker container crash mid-job: runner continues, but chat session may show stale state until
  reconnect (v2.0.0 acceptable; v2.1 will add reconciliation)
- secret cleanup failure: mark the job `failed`, warn clearly, and do not hide possible
  secret residue

### Output

- working host-side runner that creates VMs from approved chat jobs
- chat session updates with real `instance_id` and verification evidence
- chat session provides PuTTY connection details: SSH host/IP, SSH port, and username
- verify/status/evidence prompts return real data instead of "no VM attached yet"
- end-to-end test (HTTP `/ask` through real chat path) passes

### Exit Criteria

- approving a provisioning request from chat triggers actual VM creation on Windows host within
  30 seconds of approval
- AVA chat session attaches the real `instance_id` after VM creation
- AVA provides PuTTY connection details: SSH host/IP, SSH port, and username
- nginx is bootstrapped on the created VM
- `baseline_linux` hardening is applied if user accepted it
- HTTP 200 is verified from the host
- chat session shows completion status and evidence
- `tests/v2_chat_to_vm_e2e_test.py` passes by driving the full flow through HTTP `/ask` only —
  NO direct module calls
- runner survives at least one `VBoxManage` failure cleanly via Phase 7 rollback
- `v2.0.0` tags only AFTER this exit criteria is met

### Estimate

3 to 5 working days

### Open Design Questions (for user to answer before implementation)

- should the runner run as a Windows service, or as a manually-started PowerShell session for
  v2.0.0?
- should Redis credentials be passed via environment variable or config file on Windows?
- should the runner write logs to a file or to Windows Event Log?
- what's the timeout for a job between `picked_up` and `completed` before it's considered stuck
  (default proposal: 15 minutes)?
- should v2.0.0 support only one active job globally, or one active job per user?

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
9. Phase 8 — module-level end-to-end release gate (passed; reopened — chat path incomplete)
10. Phase 9 — host-side runner bridge (required user-facing release gate)

Phase 8 remains valuable as the module-level release gate.
Phase 9 is now required as the user-facing release gate.
`v2.0.0` cannot be tagged until Phase 9 exit criteria are met.

---

## Rules For Using This Plan

- finish phases in order
- do not add second-provider work inside first-provider phases
- do not add second-role work inside first-role phases
- if a phase expands unexpectedly, stop and review scope before continuing
- if code does not clearly map to one phase, it probably does not belong in `v2.0.0`
