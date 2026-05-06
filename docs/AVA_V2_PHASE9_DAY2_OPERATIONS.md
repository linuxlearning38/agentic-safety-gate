# AVA v2 Phase 9 — Day-2 Operations

Branch: `v2-development`
Status: Design locked, implementation pending
Target version: `v2.1`

## Purpose

Phase 9 Day-2 Operations turns AVA from a provisioning demo into a daily
infrastructure assistant.

Phase 9 chat-to-VM provisioning proved that AVA can create a hardened Ubuntu web
server through chat approval. The next product step is for AVA to manage that
server after creation: verify it, inspect it, restart services, collect logs,
take snapshots, and roll back safely.

This is still Phase 9 because it builds directly on the host-side runner bridge.
The runner already connects AVA chat to Windows-native VirtualBox and SSH
execution. Day-2 operations reuse that bridge for post-provisioning actions.

## Goal

Allow the user to manage AVA-created VMs from chat without manual PowerShell,
VirtualBox, or SSH troubleshooting.

AVA should answer and act like one assistant:

- know which VM is active
- know whether the runner is healthy
- know what actions require approval
- execute only through the guarded runner path
- report exact evidence after every operation
- avoid leaking internal routing confusion to the user

## What We Are Doing First

v2.1 focuses on Day-2 operations for the existing `web_server` role.

Initial supported commands:

- `show status of my web server`
- `verify the web server`
- `show nginx logs`
- `restart nginx`
- `take a snapshot before changes`
- `rollback to last snapshot`
- `stop the VM`
- `start the VM`
- `what did you do and what evidence do you have?`

These operations prove AVA can manage a server lifecycle, not just create a
server once.

## Why This Comes Before PostgreSQL

PostgreSQL is the next important server role, but Day-2 operations should come
first because every future role will need the same management layer.

If AVA can restart, inspect, snapshot, roll back, and verify one web server,
then those same patterns can be reused for:

- PostgreSQL
- Redis
- Keycloak or LDAP
- reverse proxy servers
- monitoring servers
- CI runners

Day-2 operations make AVA useful every day. New roles make AVA broader. The
daily management layer should come first.

## Scope

### In Scope For v2.1

- Manage VMs created by AVA Phase 9 provisioning.
- Use the active chat session VM by default.
- Allow explicit VM names when the user asks about a specific AVA-created VM.
- Query VirtualBox power state through the host runner.
- Query nginx status through SSH.
- Tail recent nginx/systemd logs through SSH.
- Restart nginx with approval.
- Start and stop the VM with approval.
- Take VirtualBox snapshots with approval.
- Roll back to the latest AVA-created snapshot with approval.
- Return evidence after every action.
- Keep temporary passwords unrecoverable after first issuance.
- Keep destructive or risky actions behind policy and approval.

### Out Of Scope For v2.1

- Creating PostgreSQL, Redis, auth, or proxy roles.
- Multi-VM orchestration.
- Managing arbitrary non-AVA VMs by default.
- Managing cloud providers.
- Running destructive shell commands directly from chat.
- Automatic self-healing without user-visible evidence.

## Source Of Truth

AVA should use this priority order when answering Day-2 operation prompts:

1. Runner result attached to the active provisioning session.
2. AVA-managed VM inventory/state store.
3. Live host runner query against VirtualBox.
4. Live SSH query against the guest VM, only after the VM target is known.

Qwen may explain concepts, but Qwen must not decide whether an operation is safe
or whether a VM exists. AVA owns routing, safety, and runtime truth.

## Operation Contract

Every Day-2 operation should produce structured evidence:

```json
{
  "operation_id": "<uuid>",
  "session_id": "<session_id>",
  "vm_name": "<ava-vm-name>",
  "operation": "restart_service",
  "target": "nginx",
  "approval_id": "<approval-id-or-null>",
  "status": "completed",
  "started_at": "<iso8601>",
  "completed_at": "<iso8601>",
  "evidence": {
    "precheck": "...",
    "command": "systemctl restart nginx",
    "exit_code": 0,
    "postcheck": "nginx active",
    "verification": "HTTP 200"
  },
  "error": null
}
```

The user-facing answer should summarize the evidence, not dump raw internals
unless the user asks for detail.

## Approval Policy

Low-risk read-only operations should not require approval:

- status
- verify
- show connection details
- show recent logs
- show disk/memory usage

Medium-risk operations require approval:

- restart nginx
- reload nginx
- start VM
- stop VM
- take snapshot

High-risk operations require approval plus extra warning:

- rollback snapshot
- reset VM state
- delete snapshot

Blocked operations:

- arbitrary destructive shell commands
- `rm -rf /`
- credential extraction
- disabling audit/security controls
- acting on non-AVA infrastructure without explicit support

## Runner Integration

Day-2 operations should reuse the host runner bridge instead of executing from
the Docker container.

Proposed queue extension:

- existing provisioning queue remains for create-VM jobs
- add a day-2 operation queue:
  `ava:day2:operations:requested`
- result key prefix:
  `ava:day2:operations:result:<operation_id>`
- status key prefix:
  `ava:day2:operations:status:<operation_id>`

This keeps provisioning jobs and management jobs separate while still using the
same Redis and host-runner pattern.

## Implementation Plan

1. Define Day-2 operation model and result schema.
2. Add VM inventory lookup for AVA-created instances.
3. Add runner-side handlers for read-only status and verify operations.
4. Add nginx log and nginx status handlers.
5. Add approval-gated nginx restart.
6. Add approval-gated VirtualBox snapshot creation.
7. Add approval-gated VM start/stop.
8. Add approval-gated rollback to latest AVA snapshot.
9. Add chat response formatting for operation evidence.
10. Add regression tests for routing, approval, evidence, and blocked actions.
11. Add one live validation run against an AVA-created web server.

## Expected User Experience

Example:

```text
User: restart nginx on my web server
AVA: Approval required. This will restart nginx on ava-web-23f164db.
     Approval ID: 12ab34cd

User: approve 12ab34cd
AVA: Restart completed.
     VM: ava-web-23f164db
     Service: nginx
     Precheck: active
     Action: systemctl restart nginx
     Postcheck: active
     HTTP: http://127.0.0.1:8080/ -> HTTP 200
```

Example:

```text
User: show nginx logs
AVA: Recent nginx logs from ava-web-23f164db:
     - service is active
     - no failed nginx units found
     - last access log entries returned
```

Example:

```text
User: rollback to last snapshot
AVA: High-risk approval required. This may revert VM state and discard changes
     made after the last snapshot.
```

## Exit Criteria

Phase 9 Day-2 Operations is complete when:

- AVA can identify the active AVA-created VM from chat.
- `show status` reports VirtualBox power state and connection details.
- `verify the web server` still reports runner-backed HTTP evidence.
- `show nginx logs` returns recent service/log evidence.
- `restart nginx` requires approval and verifies nginx + HTTP 200 afterward.
- `take a snapshot` requires approval and records snapshot evidence.
- `rollback to last snapshot` requires approval and verifies the VM after rollback.
- `stop VM` and `start VM` require approval and report final power state.
- blocked destructive prompts remain blocked.
- all operation responses include evidence.
- regression tests pass.
- one live VM validation passes end to end.

## Future Phases After Day-2

After Phase 9 Day-2 Operations, the next recommended product sequence is:

1. PostgreSQL DB role.
2. Redis/cache role.
3. Reverse proxy role.
4. Multi-VM web + DB orchestration.
5. Day-2 operations generalized across all roles.

