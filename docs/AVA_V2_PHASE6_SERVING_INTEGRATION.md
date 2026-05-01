# AVA v2 Phase 6 Serving Integration

Date: 2026-05-01
Branch: `v2-development`
Status: Implemented as guided `/ask` serving integration
Commit: `ad92f6e feat(v2): connect provisioning flow to serving route`

## Purpose

Phase 6 connects the v2 provisioning state machine to AVA's real serving
contract.

Before this phase, the provisioning pieces worked as modules:

- desired-state collection
- approval gating
- one-time credential issuance
- web role bootstrap
- verification and state persistence

Phase 6 makes those pieces reachable through AVA as one assistant. The user
does not need to know which internal module is running. AVA decides when a
message belongs to the guided provisioning flow and keeps the conversation in
the correct phase.

## Inputs

- Phase 2 session and desired-state model
- Phase 3 policy, approval, and credential flow
- Phase 4 web server role and bootstrap contract
- Phase 5 verification and state recording
- Existing `/ask` route in `web_agent_v2.1_guardrail.py`
- Existing deterministic router in `control/input_router.py`

## Implemented Files

- `provisioning/serving.py`
- `control/input_router.py`
- `web_agent_v2.1_guardrail.py`
- `tests/provisioning_phase6_serving_regression.py`
- `docs/AVA_V2_PHASES.md`

## Serving Contract

Phase 6 adds a thin serving adapter:

- `ProvisioningChatService`

This service owns only the user-facing guided provisioning conversation. It does
not create VMs directly. It translates chat turns into existing FSM operations.

The service can:

- start a provisioning session
- accept VM type answers
- accept CPU, RAM, disk, network, firewall, and hardening specs
- queue approval after desired state is complete
- pause while approval is pending
- continue after approval
- issue one-time temporary credentials
- accept first-login confirmation
- ask for post-login hardening choice
- move the session to the bootstrapping checkpoint

This keeps AVA's serving behavior coherent while preserving the separation:

- AVA decides the user-facing flow
- the FSM owns state transitions
- policy owns approval requirements
- credential manager owns temporary access
- later execution phases own real VM work

## Router Behavior

Phase 6 adds a controlled router intent:

- `provisioning`

Examples that now enter the guided flow:

- `I want a web server in Ubuntu`
- `I need a web server`
- `create a VM`
- `provision an Ubuntu VM`
- `nginx server in Ubuntu`

Provisioning diagrams stay on the architecture route. For example:

- `ava linux provisioning diagram`

This matters because a diagram request should never accidentally start or resume
a provisioning workflow.

## `/ask` Integration

The `/ask` route now checks the provisioning chat service through
`_resolve_controlled_query`.

The provisioning hook runs before normal architecture, troubleshooting,
definition, and general-routing resolution. It returns a normal AVA response
payload with:

- type: `knowledge`
- intent: `provisioning`
- confidence: `high`
- metadata containing provisioning session information

This means the frontend sees the same kind of response shape as other controlled
AVA paths. The internal provisioning path remains invisible to the user except
for the conversation content and metadata.

## Guided Flow

The implemented flow is:

1. User asks for a web server in Ubuntu.
2. AVA starts a provisioning session.
3. AVA asks for missing specs:
   - CPU
   - RAM
   - disk
4. User supplies specs.
5. AVA builds desired state and queues approval.
6. AVA refuses to continue while approval is pending.
7. After approval, AVA issues temporary credentials once.
8. AVA waits for first-login and password-change confirmation.
9. User confirms first login.
10. AVA offers default-on `baseline_linux` hardening.
11. User accepts or explicitly opts out.
12. AVA records the choice and moves to the bootstrapping checkpoint.

Phase 6 intentionally stops at the bootstrapping checkpoint. It does not yet run
the full VM lifecycle from the chat route. That belongs to the next integration
and rollback phases.

## Temporary Credential Boundary

Phase 6 preserves the Phase 3 credential contract:

- credentials are not issued before approval
- credentials are shown only after approval
- plaintext password is not recoverable after issuance
- repeated pending continuations do not expose a password

The user-facing response after approval includes:

- username
- temporary password
- instruction to change the password after first login
- instruction to reply after first login and password change

## Hardening Boundary

Hardening remains default-on.

After first-login confirmation, AVA says:

- `baseline_linux` is the default
- user may explicitly opt out with `skip hardening`

For v2.0.0, accepted hardening moves the session to `bootstrapping`. The actual
role bootstrap and verification execution will be connected after rollback
behavior is safe.

## Verified Behavior

Regression test:

- `tests/provisioning_phase6_serving_regression.py`

Verified:

- router detects provisioning intent
- start request is handled by the guided flow
- start request asks for missing specs
- session moves to `awaiting_specs`
- spec answer is accepted without repeating the original request
- approval is queued after CPU, RAM, and disk are collected
- desired state records CPU, RAM, and disk correctly
- continuation while approval is pending is handled safely
- pending approval does not expose credentials
- approved continuation issues one-time credentials
- approved session moves to `awaiting_first_login`
- first-login confirmation is accepted
- session moves to `awaiting_post_login_choices`
- default hardening is explained
- hardening choice moves session to `bootstrapping`
- post-login hardening action is recorded
- unrelated knowledge prompt is not hijacked
- provisioning diagram request stays on architecture/diagram route

## Tests Run

During the Phase 6 checkpoint, these were run:

- `python tests\provisioning_phase6_serving_regression.py`
- `python tests\provisioning_phase5_verification_state_regression.py`
- `python tests\provisioning_phase4_role_bootstrap_regression.py`
- `python tests\provisioning_phase3_policy_credentials_regression.py`
- `python tests\provisioning_phase2_state_regression.py`
- `python tests\serving_contract_regression.py`
- `python tests\intelligence_regression.py`
- `python tests\virtualbox_adapter_smoke.py`
- `python -m py_compile control\input_router.py provisioning\serving.py web_agent_v2.1_guardrail.py tests\provisioning_phase6_serving_regression.py`

All passed before commit `ad92f6e`.

## Exit Criteria Status

Phase 6 exit criteria:

- user can drive the `web_server` flow from AVA interface: complete through the
  bootstrapping checkpoint
- AVA can resume the conversation at the correct phase: complete
- no freeform shell shortcut bypasses the provisioning contract: complete for
  the guided chat path

Phase 6 is closed.

## Boundaries

Phase 6 does not:

- create the VM from `/ask`
- run the web role from `/ask`
- destroy failed partial VMs
- implement retain-for-debug behavior
- tag `v2.0.0`

Those belong to Phase 7 and Phase 8.
