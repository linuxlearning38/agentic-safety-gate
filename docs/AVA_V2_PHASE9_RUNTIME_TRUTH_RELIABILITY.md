# AVA v2 Phase 9.6 — Runtime Truth And Reliability

Branch: `v2-development`
Status: Slice 1 expanded
Target version: `v2.1.x`

## Purpose

Phase 9 proved that AVA can create a VirtualBox Ubuntu web server from chat.
Phase 9 Day-2 Operations proved that AVA can begin managing that server after
creation.

Phase 9.6 closes the reliability gap found during repeated reboot, retry, and
manual-delete testing: AVA must not trust one stale state store when real
infrastructure says something different.

The product rule is simple:

AVA must never claim a VM is queued, running, created, verified, or blocking a
new request unless the relevant runtime truth source supports that claim.

## Runtime Truth Sources

AVA has several truth layers. They are not interchangeable:

- **Chat/session state:** SQLite conversation checkpoint and desired state.
- **Approval state:** pending/approved/blocked action records.
- **Redis runner state:** queue, job status, runner heartbeat, operation result.
- **Host runner truth:** Windows-native process that can talk to VirtualBox and SSH.
- **VirtualBox truth:** actual VM inventory and power state.
- **Guest truth:** SSH, systemd, nginx, package, and HTTP checks inside a VM.
- **Stored evidence:** historical runner result from a previous completed action.

Stored evidence is useful history. It is not current infrastructure truth.

## Reliability Contract

Before AVA speaks about runtime state, it must resolve the correct truth source:

- AVA self/runtime questions use AVA container and dependency facts.
- Windows host, Docker Desktop, and VirtualBox questions must use host-runner
  facts when available.
- AVA-created VM questions use attached session evidence plus live host-runner
  verification when current truth is needed.
- Provisioning retry decisions must reconcile session phase, Redis job state,
  runner heartbeat, and VM attachment status.
- If Redis job state has expired while a session is non-terminal, AVA must mark
  the session failed/stale instead of blocking new provisioning forever.
- If a VM was deleted or powered off, AVA must report the live failure and must
  not recycle old HTTP 200 evidence as a current pass.
- If an action has no executable path in the current environment, AVA must not
  ask for approval as if execution will work.

## Slice 1 — Expired Runner State Must Not Block Retry

Status: implemented.

Problem found:

- A non-terminal session could have an `instance_id` attached.
- Redis status/result keys could expire.
- VirtualBox could no longer contain the VM.
- AVA still treated the session as active and blocked `I want a web server`.

Fix:

- Non-terminal sessions are allowed to expire even if `instance_id` is present.
- If the runner job status/result is missing and the session is older than the
  stale threshold, AVA marks the session `failed` with
  `runner_state_expired`.
- A fresh provisioning request is then allowed.

This prevents stale SQLite state from trapping the user after Redis expiry,
runner failure, or manual VM cleanup.

### Slice 1B — Completed History Must Not Block If The VM Was Deleted

Status: implemented.

Problem found:

- The runner had completed a previous VM job and stored successful evidence.
- The user manually deleted the VM from VirtualBox.
- The chat session was still in a follow-up checkpoint such as
  `awaiting_first_login` or `awaiting_post_login_choices`.
- AVA displayed the effective runner phase as `completed`, but still treated the
  session as an active provisioning request.
- A new `I want a web server` request was blocked even though VirtualBox had no
  AVA-created web server left.

Fix:

- A completed runner result now makes the provisioning execution terminal for
  the active-request guard.
- Existing completed VM history blocks accidental duplicate creation only after
  a live host-runner verification confirms the VM still exists and is running.
- If live verification says the VM is missing or not running, AVA treats the old
  stored evidence as history and allows a fresh provisioning request.
- If live verification cannot return a clear answer before the guard timeout,
  AVA must not use stored history as proof that the old VM still exists.

This preserves the safety guard against accidental duplicate web servers while
removing the stale-history trap after manual VM deletion.

### Slice 1C — Hostname Reuse Must Not Reuse Protected Runner Keys

Status: implemented.

Problem found:

- A user deleted `ava-web-01` and requested a new VM with the same hostname.
- The runner generated a new job, but retained the `ava-runner` SSH private key
  at a hostname-derived path:
  `.ava-runner/keys/ava-web-01_ava_runner_ed25519`.
- A protected stale key from the previous VM could not be overwritten on
  Windows, causing provisioning to fail with `Permission denied`.

Fix:

- Retained runner keys are now stored with a job-derived filename:
  `.ava-runner/keys/<runner-job-id>_ava_runner_ed25519`.
- Day-2 operations and the browser console look up the job-specific key before
  legacy hostname-derived key paths.
- Inaccessible legacy key candidates are skipped instead of crashing key lookup.

This lets users safely reuse normal hostnames such as `ava-web-01` without
manually cleaning hidden runner key files.

## Next Slices

### Slice 2 — Host Capability Preflight

Before issuing a provisioning password or queueing a VM job, AVA should verify:

- runner heartbeat is healthy
- VirtualBox is reachable from the host runner
- base template VM exists
- NAT ports are available
- no orphaned operation already owns the target ports

If preflight fails, AVA should explain the failing dependency and avoid queueing
the job.

### Slice 3 — Live Inventory Reconciliation

AVA should maintain or query an AVA-managed VM inventory:

- session ID
- job ID
- VM name
- VirtualBox UUID
- power state
- SSH host/port
- HTTP port
- last live verification timestamp
- latest known failure

This inventory should be refreshed by the host runner, not guessed by Qwen.

### Slice 4 — Environment-Aware Actions

AVA must choose the right execution boundary:

- container Linux actions run inside `ava-agent`
- Windows host actions run through the host runner
- guest VM actions run over retained runner SSH identity
- unsupported actions fail before approval

Example:

`restart docker service` must not call Linux `systemctl` inside the container
for Docker Desktop. It should route to a supported Windows host action or say
that host-runner support is not available yet.

### Slice 5 — Product-Grade Reliability Matrix

Regression coverage should include:

- runner offline
- Redis expired
- stale session with attached VM name
- VM manually deleted
- VM powered off
- template missing
- NAT port collision
- DNS/bootstrap failure
- approval replay
- typo status/diagram/provisioning prompts
- stored evidence vs live verification
- host action unsupported boundary

## Product Principle

AVA is the decision-maker. Qwen may reason, but Qwen must never decide runtime
truth. Runtime truth belongs to AVA's resolver, state stores, host runner, and
live evidence.
