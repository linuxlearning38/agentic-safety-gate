# AVA v2 Architecture

Date: 2026-04-29
Status: Draft architecture direction
Target release family: `v2.x`

## Positioning

AVA v2 is not a replacement for Terraform, Ansible, AWS, Azure, or GCP.

AVA v2 is an **AI Infrastructure Lifecycle Operator**:

- accepts infrastructure intent in natural language
- asks for missing specifications through guided conversation
- applies policy before any action
- provisions through a provider adapter
- verifies outcomes instead of assuming success
- continues into post-provision hardening and service flow

Provisioning is the first proof surface, not the whole product.

---

## Product Direction

The point of AVA v2 is not:

- “I can create a VM”
- “I can automate a cloud task”
- “I can compete with Ansible or Terraform”

The point of AVA v2 is:

- AVA behaves like a guided infrastructure operator
- AVA collects the right inputs from the user
- AVA enforces security and approval boundaries
- AVA verifies what it created
- AVA explains what happened in operator-friendly terms

This makes provisioning a strong first use case for AVA’s agent behavior.

---

## v2.0.0 Scope

The first v2 slice is intentionally one complete vertical flow only:

- Provider: `VirtualBox`
- OS: `Ubuntu`
- Role: `Web Server`
- Interaction model: guided multi-turn conversation
- Lifecycle:
  - user request
  - role selection
  - specification gathering
  - policy and approval
  - VM creation
  - temporary access delivery
  - first-login confirmation
  - optional hardening
  - verification
  - completion report

### Explicitly Out of Scope for v2.0.0

- AWS, Azure, GCP providers
- database server role
- load balancer role
- generic service marketplace
- autonomous healing loops
- fleet management
- drift remediation
- multi-VM orchestration

This is a hard scope lock, not a suggestion.

---

## Decisions Locked

1. **Provisioning source: Ubuntu cloud image / template (not ISO install)**  
   Justification: 2-3 minute provision time is sustainable for iteration; 15-25 minute ISO installs are not.

2. **Conversation engine: Custom FSM + SQLite (not LangGraph)**  
   Justification: An 11-phase flow does not justify LangGraph overhead; FSM + SQLite is sufficient, inspectable, and easier to debug.

3. **Credential flow: Temporary username + password, displayed once, forced password change on first login. SSH-key-only mode deferred to v2.x.**  
   Justification: This matches the intended UX. Future modes like SSH-key, passwordless, or enterprise workflows can layer on later.

4. **Login verification: User self-confirmation in AVA UI is sufficient for v2.0.0. Backend corroboration is deferred.**  
   Justification: Polling-based verification is brittle and would create a fake bottleneck for the first slice.

5. **Hardening: Default-on `baseline_linux` profile. Opt-out requires an explicit user confirmation phrase.**  
   Justification: This keeps the security posture strong and aligns with AVA’s security-first positioning.

6. **Service scope: Role-defined services only. No generic service installer.**  
   Justification: This prevents unbounded scope. Generic service installation belongs in a later phase or never.

7. **Rollback on failure: Destroy partial VM by default. User may explicitly choose to retain it for debugging.**  
   Justification: Clean rollback is the safe default, while the retain option preserves diagnostic value when needed.

8. **v2.0.0 success criterion: A single repeatable end-to-end test (`tests/v2_e2e_test.py`) executes the full guided conversational flow, produces a working nginx VM, verifies HTTP 200, includes a complete audit log of each step, and finishes in under 10 minutes wall time.**  
   Justification: This creates a concrete pass/fail boundary. Tag `v2.0.0` only when this passes.

## Execution Rules

Build discipline (non-negotiable for `v2.0.0`):

- v1 is frozen. Only critical security/correctness bugs get patched on `master`.
- All v2 work happens on a single branch: `v2-development`.
- Scope: VirtualBox + Ubuntu + `web_server` only.
- No second provider until `v2.0.0` is tagged.
- No second role until `v2.0.0` is tagged.
- No tag until `tests/v2_e2e_test.py` passes.
- No "while I'm here" feature additions during a phase.
- Every phase has documented: input, output, exit criteria.

Operational rules:

- Codex implements; Claude CLI verifies; user directs.
- No commits between 11 PM and 6 AM IST.
- No new phase begins until current phase exit criteria are met.
- If a phase takes 2x estimate, stop and review before continuing.

---

## Primary User Flow

### Phase 1 — Guided Provisioning

User:

`I want a VM`

AVA:

1. asks what kind of VM is needed
2. offers a role menu, starting with:
   - web server
3. asks for missing specifications:
   - CPU
   - RAM
   - disk size
   - network mode
   - firewall exposure
4. builds a structured desired state
5. runs policy checks
6. requests approval
7. creates the VM
8. returns temporary access information
9. asks the user to log in and change the temporary password

### Phase 2 — Post-Provision Continuation

Once first access is confirmed, AVA continues:

1. confirms timestamp and state transition
2. asks whether the user wants role-specific service setup
3. asks whether the user wants hardening
4. applies the selected actions
5. runs verification
6. returns a completion report

This is a sequential, stateful conversation. It is not a single stateless command.

---

## Core Pipeline

Every provisioning request flows through the same pipeline:

`Intent -> Session State -> Desired State -> Policy -> Approval -> Provider Adapter -> Bootstrap -> Verification -> Report`

### 1. Intent Layer

AVA interprets the request and decides:

- is this a provisioning request?
- is the role already known?
- what information is missing?
- should AVA ask the next question, or move to approval?

AVA remains the decision-maker. The model is only a reasoning engine when AVA selects it.

### 2. Session State

Provisioning is multi-turn and must survive:

- browser refresh
- user pause/resume
- multiple in-progress sessions
- partial answers

Session state must be persisted, not held only in memory.

### 3. Desired State

Every request is normalized into a structured desired-state object.

Example fields:

- `provider`
- `os`
- `role`
- `cpu`
- `ram_gb`
- `disk_gb`
- `network_mode`
- `firewall_profile`
- `hardening_profile`
- `post_login_actions`

The desired state is the execution contract for the provisioning engine.

### 4. Policy

Policy evaluates:

- whether the requested configuration is allowed
- whether approval is required
- whether exposure rules are acceptable
- whether hardening must be enforced

OPA remains the policy authority.

### 5. Approval

Provisioning is approval-gated by default in `v2.0.0`.

No VM creation starts until approval exists.

### 6. Provider Adapter

Provider-specific logic lives behind one contract.

`v2.0.0` implements `VirtualBox` only.

### 7. Bootstrap

Once the VM is reachable, AVA applies role bootstrap and optional hardening.

### 8. Verification

AVA verifies actual state:

- VM exists
- VM is running
- network is reachable
- SSH works
- role service is installed
- role service is active
- expected port is open
- expected health response is present

### 9. Report

AVA returns:

- completion status
- timestamp
- credentials/access reminders
- verification evidence
- next recommended step

---

## Proposed Module Layout

Create a dedicated `provisioning/` package.

```text
provisioning/
  __init__.py
  conversation/
    __init__.py
    session_manager.py
    flow_engine.py
    prompts.py
  desired_state.py
  policy.py
  credentials.py
  adapters/
    __init__.py
    base.py
    virtualbox.py
  roles/
    __init__.py
    base.py
    web_server.py
  bootstrap/
    __init__.py
    ssh_executor.py
  verify/
    __init__.py
    engine.py
  state/
    __init__.py
    store.py
```

### Responsibility Split

- `conversation/`
  - question/answer flow
  - session persistence hooks
  - next-step selection
- `desired_state.py`
  - dataclass and validation
- `policy.py`
  - provisioning-specific policy evaluation
- `credentials.py`
  - temporary access generation and lifecycle helpers
- `adapters/virtualbox.py`
  - VirtualBox CLI implementation
- `roles/web_server.py`
  - role defaults, bootstrap actions, verification expectations
- `bootstrap/ssh_executor.py`
  - post-create SSH execution
- `verify/engine.py`
  - role-aware verification checks
- `state/store.py`
  - persistent records for sessions and provisioned instances

---

## Session Model

Provisioning requires persistent conversation state.

Suggested session phases:

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

Each session should track:

- `session_id`
- `user_id`
- `phase`
- `role`
- `provider`
- `collected_answers`
- `desired_state`
- `approval_id`
- `instance_id`
- `created_at`
- `updated_at`

SQLite is enough for `v2.0.0`.

---

## Desired State Model

Initial v2 desired state for the first slice:

```text
provider = virtualbox
os = ubuntu
role = web_server
cpu = <int>
ram_gb = <int>
disk_gb = <int>
network_mode = nat | bridged | hostonly
firewall_profile = web_public | internal_only
hardening_profile = none | baseline_linux
```

Rules:

- AVA asks only for missing required fields
- defaults should exist where safe and documented
- invalid combinations are blocked before adapter execution

---

## Provider Adapter Contract

Every provider must implement the same contract.

```text
plan_instance(desired_state) -> plan
create_instance(plan) -> instance_id
start_instance(instance_id) -> status
stop_instance(instance_id) -> status
destroy_instance(instance_id) -> status
configure_network(instance_id, network_spec) -> status
inject_access(instance_id, access_spec) -> status
get_instance_state(instance_id) -> provider_state
get_connection_info(instance_id) -> host/port/user
```

### VirtualBox Adapter Notes

`v2.0.0` should use `VBoxManage` CLI directly.

Key responsibilities:

- create VM
- attach Ubuntu base image
- configure CPU/RAM/disk
- configure NIC mode
- start VM
- return instance identity and connection details

VirtualBox is the testable local provider for the first slice.

---

## Role Contract

Roles define what AVA is creating, not just how the VM is created.

Initial role:

- `web_server`

Role fields:

- `packages`
- `services`
- `ports`
- `bootstrap_steps`
- `verification_checks`
- `hardening_compatible`
- `post_login_questions`

### Web Server Role

First implementation should stay narrow:

- install nginx
- enable nginx
- expose expected web port
- verify HTTP response

No generic service marketplace in `v2.0.0`.

---

## Credentials and First Login

AVA needs a temporary-access strategy.

For `v2.0.0`, the simplest practical model is:

- generate temporary credentials or access secret
- display once to the user
- instruct password change on first login
- track that AVA is waiting for first-login confirmation

### First-Login Verification

AVA should not overpromise perfect identity verification.

For `v2.0.0`, the practical goal is:

- confirm that first access happened
- confirm that the lifecycle can continue

Initial acceptable implementation:

- user confirms first login in AVA
- AVA optionally corroborates through SSH polling or auth-log signal where feasible

This should be designed as a workflow checkpoint, not fake certainty.

---

## Hardening Profile

“Harden the server” must mean a named profile.

Initial profile:

- `baseline_linux`

The profile should be documented and deterministic.

Example baseline scope:

- enforce non-root operational use where possible
- configure firewall for the chosen role
- disable unnecessary exposure
- ensure the role service starts correctly after hardening

Hardening must be role-aware and verification-backed.

---

## Verification Engine

Verification is mandatory.

AVA must confirm:

- VM creation success
- runtime state is `running`
- connection path works
- role bootstrap completed
- nginx is active
- expected port is reachable
- expected response returns

Verification output should include:

- check name
- pass/fail
- evidence
- timestamp

No “assume success” path.

---

## State Store

Use SQLite first.

Store at least:

- provisioned instances
- active sessions
- desired state
- actual observed state
- verification status
- last completion timestamp
- lifecycle outcome

The store is operational memory, not analytics infrastructure.

---

## Integration With Existing AVA

What stays with current AVA:

- routing
- approval system
- security policy model
- audit logging
- serving contract

What gets added:

- provisioning intent path
- conversation state tracking
- provisioning execution modules
- post-provision continuation flow

AVA remains the assistant. Provisioning is an added lifecycle capability under AVA’s control.

---

## v2.0.0 Success Criteria

The first release is successful if this full flow works:

1. user asks for a VM
2. AVA asks which VM type
3. user chooses `web server`
4. AVA asks for specifications
5. approval is required and enforced
6. AVA provisions Ubuntu in VirtualBox
7. AVA returns temporary access information
8. user confirms first login and password change
9. AVA offers hardening
10. AVA applies nginx + optional baseline hardening
11. AVA verifies service health
12. AVA returns a completion report with evidence

If that works cleanly, v2 has a real foundation.

---

## Non-Goals for This Architecture

This document does not authorize:

- more providers in the first slice
- more roles in the first slice
- self-healing in the first slice
- generic package/service installation in the first slice
- freeform infrastructure orchestration on `master`

One provider, one role, full lifecycle.

That is the rule.

---

## Architecture Addendum: Current Serving Contract (2026-06-15)

AVA now has a stricter product contract than the first architecture slice:
AVA must choose the answer mode before producing an answer.

AVA owns:

- exact self/runtime answers
- grounded DevOps knowledge answers
- Qwen reasoning delegation
- provisioning conversation state
- approval and policy boundaries
- guarded operational actions
- Web Console and server-management routing
- runtime truth reconciliation

Qwen remains a reasoning engine. It is not the owner of runtime truth, policy,
routing, or approval decisions.

Runtime truth now comes from layered evidence:

- durable provisioning sessions and stored runner results
- Redis runner status, progress, heartbeat, and operation signals
- live host-runner checks
- VirtualBox VM state
- guest SSH and HTTP verification

Product rule:

- old chat history and stored evidence may explain what happened before
- live checks decide what is true now when AVA is about to block, operate on, or
  open a console to a server
- if live truth is unavailable, AVA must say what dependency is missing instead
  of pretending the state is certain

This is the current separation:

- AVA is the decision-maker and safety boundary.
- Qwen is the reasoning helper.
- The host runner is the execution bridge.
- VirtualBox and the guest VM are managed targets.
