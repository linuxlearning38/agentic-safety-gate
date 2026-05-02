# AVA v2 Phase 9 Runner Bridge Design

Date: 2026-05-02
Branch: `v2-development`
Status: Design locked, implementation pending

## Purpose

Phase 9 closes the gap discovered during live validation on 2026-05-02.

The Phase 8 e2e test (`tests/v2_e2e_test.py`) runs Python natively on Windows and calls
`VBoxManage` directly. It proves the provisioning modules work. It does not prove the
user-facing flow works.

The user-facing flow goes through `/ask` in AVA chat, which runs inside the Docker container.
The container cannot reach `VBoxManage` on the Windows host. Live validation confirmed that
after a user approves a provisioning plan in chat, no VM is created. The conversation advances
but the infrastructure does not.

Phase 9 bridges this by introducing a host-side runner: a process that runs natively on Windows,
watches a Redis queue for approved jobs, executes the same module sequence that Phase 8 already
proved, and writes results back to AVA state so that chat sessions reflect reality.

`v2.0.0` does not tag until Phase 9 exit criteria are met.

## Inputs

- Phase 6 serving integration (chat path through `/ask`)
- Phase 7 rollback manager (provider-agnostic failure handling)
- Phase 8 e2e module integration (proves modules work natively on Windows)
- Existing Redis instance (already in `docker-compose`)
- Existing host-side `VBoxManage` path (already proven by `tests/v2_e2e_test.py`)

## Architecture

```
  User browser
       |
       v
  AVA chat (/ask)        [Docker container]
  provisioning/serving.py
       |
       | enqueue job after approval
       v
  Redis queue                [Docker container]
  ava:provisioning:jobs:approved
       |
       | BLPOP (host-side poll)
       v
  host_runner.py             [Windows host, native Python]
       |
       | executes module sequence:
       |   VirtualBox adapter -> cloud-init -> SSH bootstrap
       |   -> nginx role -> baseline_linux hardening -> verify engine
       v
  VirtualBox / VBoxManage    [Windows host]
       |
       | writes result after completion/failure
       v
  Redis result keys          [Docker container]
  ava:provisioning:jobs:result:<job_id>
  ava:provisioning:jobs:status:<job_id>
       |
       | read on verify/status/evidence prompts
       v
  AVA chat (/ask)
  provisioning/serving.py
       |
       v
  User browser (real instance_id, evidence, HTTP 200 confirmation)
```

## Job Lifecycle State Machine

```
queued
  |
  v
picked_up       <- runner claims the job
  |
  v
provisioning    <- VirtualBox clone created
  |
  v
bootstrapping   <- SSH connected, nginx/ufw installed
  |
  v
hardening       <- baseline_linux applied (if accepted by user)
  |
  v
verifying       <- verification engine running
  |
  +---> completed   <- HTTP 200 verified, evidence written
  |
  +---> failed      <- any phase failure; Phase 7 rollback applied
```

`cancelled` is written by the chat path if the user cancels before the runner picks up the job.

Once a job moves to `picked_up`, it cannot be cancelled through chat (v2.0.0 limitation).

## Redis Key Contract

| Key | Purpose |
| --- | --- |
| `ava:provisioning:jobs:approved` | list; RPUSH to enqueue, BLPOP to pick up |
| `ava:provisioning:jobs:status:<job_id>` | string; current lifecycle state |
| `ava:provisioning:jobs:result:<job_id>` | string (JSON); final result written on completion or failure |

### Job Message (enqueued by serving.py)

```json
{
  "job_id": "<uuid>",
  "session_id": "<session_id>",
  "desired_state": { ... },
  "credentials_seed_data": {
    "credential_id": "<credential_id>",
    "username": "...",
    "temporary_password": "<short-lived provisioning secret>"
  },
  "enqueued_at": "<iso8601>",
  "expires_at": "<iso8601>"
}
```

`temporary_password` is intentionally a short-lived provisioning secret, not a durable stored
credential. The host runner needs it to build the cloud-init seed for the VM. It must not appear
in result messages, logs, status output, or evidence output.

### Result Message (written by host_runner.py)

```json
{
  "job_id": "<uuid>",
  "instance_id": "<vbox-vm-name>",
  "instance_name": "<hostname>",
  "ssh_host": "127.0.0.1",
  "ssh_port": 2222,
  "http_port": 8080,
  "verification_evidence": { ... },
  "completion_timestamp": "<iso8601>",
  "error": null
}
```

On failure, `instance_id` may be null if the VM was never created or was rolled back.
`error` contains the failure class and message.

## Credential Handling Contract

- Redis may contain recoverable temporary passwords only inside `credentials_seed_data`, only
  until the runner builds the cloud-init seed or the job expires.
- The approved-job queue entry and any related secret-bearing status must have a TTL. Default:
  30 minutes.
- Result messages must never include temporary passwords.
- Runner logs must never print temporary passwords or rendered cloud-init user-data.
- Host-side cloud-init seed files containing secrets must be deleted after the VM starts unless
  retain-debug is explicitly enabled.
- If seed cleanup fails, the runner must mark the job `failed`, warn clearly, and avoid hiding
  possible secret residue.
- AVA chat may display the temporary password once to the user. Later status/evidence prompts
  must report only `credential issued: yes/no`.

## Host Runner Contract

- runs as a long-lived Python process on Windows host — NOT inside Docker
- polls Redis using `BLPOP ava:provisioning:jobs:approved <timeout>` (timeout 30s)
- picks up one job at a time; single-worker for v2.0.0 (multi-worker is v2.1+)
- after picking up a job, writes `status = picked_up` immediately
- executes the same module sequence that `tests/v2_e2e_test.py` executes today:
  - `VirtualBoxAdapter.create_instance`
  - cloud-init seed injection
  - VM start
  - SSH reachability confirmation
  - `WebServerRole.bootstrap`
  - `baseline_linux` hardening (if `hardening_profile != none`)
  - `VerificationEngine.verify`
- writes status to Redis after each phase transition
- writes final result to Redis on completion or failure
- on failure, invokes `ProvisioningRollbackManager` before writing the failed result
- crash recovery: on restart, skips any job already at `picked_up` or beyond (job becomes
  orphaned; manual cleanup required for v2.0.0)
- logs every action to a host-side log file (path configurable via environment variable)

## AVA Serving Integration Points

Two changes are required in `provisioning/serving.py`:

1. **Enqueue on approval**: after approval is accepted and temporary credentials are issued,
   push the job message to `ava:provisioning:jobs:approved`.

2. **Read on follow-up prompts**: when the session is in `bootstrapping` or later and the user
   sends verify/status/evidence prompts, read the current status and result from Redis instead
   of reporting "no VM attached yet". When the result is present, attach `instance_id` to the
   session and return real evidence.

## Failure Handling

| Scenario | Behavior |
| --- | --- |
| Runner crash mid-job | Job orphaned at `picked_up`; manual cleanup required for v2.0.0 |
| `VBoxManage` failure | Phase 7 rollback applied; `failed` result written; next job proceeds |
| Redis connection lost | Runner exits cleanly; requires manual restart |
| Docker container crash mid-job | Runner continues; chat session shows stale state until reconnect (v2.0.0 acceptable) |
| User cancels before `picked_up` | serving.py writes `cancelled` status; runner skips the job |
| Secret cleanup failure | Job marked `failed`; warning reports possible secret residue without printing the secret |

## Implementation Order

1. **Redis job queue contract** — define key names, message format, and TTL policy; no code yet,
   just the spec (this document is that spec).
2. **Result writer interface** — `provisioning/runner/result_writer.py`; writes status and result
   keys; unit-testable without a real runner.
3. **Host runner skeleton** — `provisioning/runner/host_runner.py`; poll loop, job pick-up,
   status writes; no execution logic yet.
4. **Wire serving.py to enqueue on approval** — after credential issuance, push job to Redis.
5. **Wire serving.py status/verify/evidence to read from Redis** — replace "no VM attached yet"
   with real result data when available.
6. **Connect host runner to existing module sequence** — replace stub with the real execution
   path from `tests/v2_e2e_test.py`.
7. **Phase 9 regression test** — `tests/provisioning_phase9_runner_bridge_regression.py`;
   covers queue contract, result writer, and serving integration with a mocked runner.
8. **Full chat-to-VM e2e test** — `tests/v2_chat_to_vm_e2e_test.py`; drives the full flow
   through HTTP `/ask` only; NO direct module calls; asserts HTTP 200 from the created VM.
9. **Update Phase 9 exit criteria status** in `docs/AVA_V2_PHASES.md` when each step lands.

## Exit Criteria

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

## Open Design Questions (for user to answer before implementation starts)

1. Should the runner run as a Windows service, or as a manually-started PowerShell session for
   v2.0.0?
2. Should Redis credentials be passed via environment variable or config file on Windows?
3. Should the runner write logs to a file or to Windows Event Log?
4. What is the timeout for a job between `picked_up` and `completed` before it is considered
   stuck? (Default proposal: 15 minutes.)
5. Should v2.0.0 support only one active job globally, or one active job per user?

## Go / No-Go Rule

Implementation does NOT start until the user accepts this design.

If implementation discovers that one of these assumptions is wrong, stop and update this document
before continuing.
