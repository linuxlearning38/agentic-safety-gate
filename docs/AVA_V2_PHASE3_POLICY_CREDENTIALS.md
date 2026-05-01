# AVA v2 Phase 3 Policy, Approval, And Credential Flow

Date: 2026-05-01  
Branch: `v2-development`  
Status: Implemented as a standalone regression checkpoint
Commit: `427cb10 feat(v2): add provisioning approval and credential flow`

## Purpose

Phase 3 connects the guided provisioning state machine to AVA's safety model.

The goal is simple:

- AVA must not provision without approval.
- AVA must issue temporary access safely.
- AVA must pause at first-login confirmation before post-login actions.

This phase does not create or bootstrap a VM. It builds the approval and
credential gate that later phases must pass through.

## Inputs

- Phase 2 desired-state model
- Phase 2 SQLite-backed session manager
- Existing AVA approval queue in `control/approval.py`
- Existing v1 policy posture where medium/high-risk actions require approval

## Implemented Files

- `provisioning/policy.py`
- `provisioning/credentials.py`
- `provisioning/conversation/flow_engine.py`
- `provisioning/conversation/session_manager.py`
- `tests/provisioning_phase3_policy_credentials_regression.py`

## Policy Contract

Phase 3 introduces a deterministic provisioning policy check.

Allowed v2.0.0 desired state:

- provider: `virtualbox`
- OS: `ubuntu`
- role: `web_server`

Any desired state outside that slice is blocked before approval.

For the allowed slice, the policy decision is:

- effect: `require_approval`
- risk: `medium`
- reason: provisioning changes local infrastructure and requires operator
  approval

This keeps provisioning aligned with AVA's existing guarded-action model.

## Approval Flow

Phase 3 uses AVA's existing approval queue instead of creating a second approval
system.

The approval action is:

- `provision_virtualbox_ubuntu_web_server`

The approval key is stable per session:

- `provisioning:provision_virtualbox_ubuntu_web_server:<session_id>`

Approval metadata includes:

- session id
- user id
- desired state
- policy effect
- policy reason

State transition:

1. Phase 2 completes desired-state collection.
2. Session enters `awaiting_approval`.
3. AVA queues an approval request.
4. If approval is still pending, provisioning continuation is blocked.
5. After approval, AVA advances the session to `awaiting_first_login`.

## Credential Flow

Phase 3 implements temporary credential issuance for the v2 flow.

Credential behavior:

- username defaults to `avaadmin`
- temporary password is generated with mixed character classes
- password is returned once at issuance time
- password is not recoverable after issuance
- only hash, salt, metadata, and display/audit timestamps are stored
- user must change the password after first login

Stored credential metadata:

- credential id
- session id
- username
- password hash
- salt
- creation timestamp
- displayed timestamp
- `must_change_password`

The plaintext temporary password is intentionally not stored.

## First-Login Checkpoint

After approval and credential issuance, the session moves to:

- `awaiting_first_login`

For v2.0.0, the first-login continuation is user-confirmed. The user confirms
that they logged in and changed the temporary password.

After confirmation, AVA moves the session to:

- `awaiting_post_login_choices`

This is the handoff point to Phase 4 and later post-login bootstrap/hardening
work.

## Verified Behavior

Regression test:

- `tests/provisioning_phase3_policy_credentials_regression.py`

Verified:

- complete specs move a session to `awaiting_approval`
- provisioning policy evaluates the allowed slice as `require_approval`
- unsupported desired state is blocked before approval
- approval requests are created in AVA's existing approval queue
- queued approval starts as `pending`
- pending approval blocks provisioning continuation
- approved request advances to `awaiting_first_login`
- temporary credential is issued once
- credential id is stored on the session
- username is stored for later cloud-init use
- credential metadata can be reloaded
- plaintext password is not recoverable after issuance
- stored hash verifies the original one-time password
- repeated continuation does not reissue or re-expose credentials
- first-login confirmation advances to `awaiting_post_login_choices`

## Tests Run

During the Phase 3 checkpoint, these were run:

- `python tests\provisioning_phase2_state_regression.py`
- `python tests\provisioning_phase3_policy_credentials_regression.py`
- `python tests\virtualbox_adapter_smoke.py`
- `python tests\virtualbox_cloud_init_access_smoke.py`

All passed before commit `427cb10`.

## Exit Criteria Status

Phase 3 exit criteria:

- AVA cannot provision without approval: complete
- temp credentials can be issued and consumed by the flow: complete
- AVA can pause at `awaiting_first_login` and resume after user confirmation:
  complete

Phase 3 is closed.

## Boundaries

Phase 3 does not:

- execute VirtualBox provisioning
- write cloud-init seed media
- install nginx
- apply firewall rules
- verify HTTP 200
- integrate the full user-facing AVA UI

Those belong to later phases.
