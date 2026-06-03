# AVA v2 Phase 9.6 — Runtime Truth And Reliability

Branch: `v2-development`
Status: Slice 1 started
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
