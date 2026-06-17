# AVA v2 Phase 9 — Chat-to-VM Provisioning: Operations Guide

Branch: `v2-development`
Status: Functional — Phase 9.5 operational hardening and May 5-6 product polish complete

Next phase document: `docs/AVA_V2_PHASE9_DAY2_OPERATIONS.md`

## Operator Quickstart

**One-time setup (run once per Windows user account):**
```powershell
.\scripts\check-ava-storage.ps1
.\scripts\migrate-ava-data-to-volume.ps1      # dry-run only; optional
.\scripts\install-runner-task.ps1
```
The storage check is non-destructive. The migration script is dry-run by
default and does not copy anything unless run later with `-Execute` and an
explicit `YES` confirmation. The runner installer creates a current-user
Startup-folder hook that runs `start-ava.ps1` at login; that startup script
waits for Docker, starts AVA, and then starts the host runner. Where Windows
permits it, the installer also creates a daily cleanup task at 03:00.

**Daily startup (or after any reboot):**
```powershell
.\scripts\start-ava.ps1
```
That is the only command needed.  It:
1. Detects and waits for Docker; starts Docker Desktop if needed
2. Auto-detects the current WSL2 IP and writes `OLLAMA_HOST` to `.env`
3. Verifies the product data volume exists; if legacy WSL data is present but
   `ava_data` is missing, startup stops and asks for migration first
4. Brings up all containers with `docker compose up -d`
5. Polls `https://localhost:5443/health` until healthy (or 120 s timeout)
6. Starts the host runner in a background minimised window

**Verify everything is up:**
```powershell
docker ps                               # all containers healthy
curl -k https://localhost:5443/health   # {"status":"ok",...}
```

---

## Current Product Behavior (Validated 2026-06-11)

AVA now supports the full user-facing chat-to-VM loop for a VirtualBox Ubuntu
web server:

1. User asks for a web server.
2. AVA asks for missing VM specs (`cpu`, `ram_gb`, `disk_gb`) and accepts an
   optional hostname.
3. AVA prepares a plan and tells the user exactly how to approve it:
   `approve <approval_id>`.
4. Approval records consent only. AVA does not create a VM, issue a temporary
   password, or queue the runner job at this step.
5. After approval is recorded, AVA instructs the user to reply
   `continue provisioning` to start execution.
6. On `continue provisioning`, AVA checks that the Windows host runner heartbeat
   is healthy. If not, AVA refuses to issue credentials or queue the VM.
7. If the runner is healthy, AVA issues the temporary password once, queues the
   runner job, and tells the user that VM provisioning is in process.
8. Runner creates the VM through `VBoxManage`, waits for cloud-init, bootstraps
   nginx, applies `baseline_linux`, verifies guest and host HTTP 200, and writes
   result evidence to Redis.
9. AVA status and evidence prompts show the current runner stage, expected time
   guidance, and the connection details as soon as the runner exposes them.
10. AVA Web Console is intentionally held until provisioning has completed and
    live verification evidence is available.
11. After completion, AVA tells the user that Web Console access is available
    and the user can type `open web console`.

Validated live result:

| Field | Value |
|------|-------|
| VM instance | `ava-web-23f164db` |
| Runner job | `23f164db-8fa5-4245-a686-f2beecf29dca` |
| SSH / PuTTY | `127.0.0.1:2222` |
| Username | `avaadmin` |
| Web URL | `http://127.0.0.1:8080/` |
| Runner status | `completed` |
| HTTP evidence | guest curl success and host HTTP 200 |
| Hardening | `baseline_linux` applied by runner |

Validated chat prompts:

- `show me the provisioning status`
- `continue provisioning`
- `open web console`
- `how do I connect with PuTTY?`
- `verify the web server`
- `what did you do and what evidence do you have?`
- misspelled status prompt: `show me the provisionning status`
- late follow-up prompt after completion: `I logged in and changed the password`
- late hardening prompt after completion: `yes harden it`

The last two prompts are intentionally absorbed by completed provisioning
sessions. AVA should not fall back to the old generic scope response. If the VM
is already complete, AVA records/acknowledges the follow-up and explains that
the runner already completed the VM, applied hardening, bootstrapped nginx, and
verified HTTP 200.

---

## Architecture

```
User browser
     |
     v
AVA chat (/ask)             [Docker container ava-agent]
provisioning/serving.py
     |  approve consent, then continue provisioning
     |  enqueue job only after runner heartbeat is healthy
     v
Redis queue                 [Docker container agent_redis]
ava:provisioning:jobs:approved
     |  BLPOP (host-side poll)
     v
host_runner.py              [Windows host, native Python]
     |  module sequence: VirtualBox -> cloud-init -> SSH -> nginx -> verify
     v
VirtualBox / VBoxManage     [Windows host]
     |  result written to Redis
     v
AVA chat (/ask)             -- session shows real instance_id and evidence
```

---

## Runner Readiness Preflight

AVA may record approval as user consent even if the Windows host-side runner is
not alive. It must not start provisioning, issue a password, or queue a VM job
until the runner is healthy. Otherwise the user sees a password and a queued
job, but no VM is created.

Phase 9 now treats the runner as a required dependency before execution:

1. `host_runner.py` writes a Redis heartbeat to
   `ava:provisioning:runner:heartbeat`.
2. The heartbeat has a 90-second TTL, so stale runners expire automatically.
3. `provisioning/serving.py` records approval as consent and waits for the user
   to say `continue provisioning`.
4. On `continue provisioning`, AVA checks the heartbeat before creating any
   credentials or queueing the runner job.
5. If the heartbeat is missing or stale, AVA refuses to start provisioning,
   keeps the approved plan available, does not queue a VM job, and does not
   print a temporary password.
6. The user can start AVA with `.\scripts\start-ava.ps1`, wait for the runner
   to report healthy, then say `continue provisioning` again.

This is intentional product behavior: runner first, VM creation second.

---

## Provisioning Progress UX

While provisioning is running, AVA should explain exactly what is happening
instead of leaving the user guessing. Status responses include runner progress
when available:

| Stage | User-facing meaning | Typical guidance |
|-------|---------------------|------------------|
| `queued` | Job is waiting for the host runner | usually picked up in under 1 minute |
| `picked_up` / `provisioning` | Runner is preparing the VM | usually 3-8 minutes total |
| `vm_started` | VirtualBox VM has started | SSH usually becomes ready in 1-3 minutes |
| `ssh_ready` / `cloud_init` | SSH is reachable; cloud-init is finishing | usually 2-6 minutes |
| `bootstrapping` | nginx and role setup are running | usually 1-4 minutes |
| `hardening` | baseline hardening is being applied | usually 1-3 minutes |
| `verifying` | AVA is checking SSH and HTTP evidence | usually under 1 minute |
| `completed` | VM is ready | Web Console can be opened |
| `failed` | Provisioning failed | AVA reports the failure class and cleanup state |

The browser also starts a lightweight background watcher after provisioning is
queued. It checks status periodically and posts the final completed or failed
state into chat, so the user does not need to keep asking manually.

Web Console gating is strict:

- Progress-only connection details are shown as early visibility, not as final
  access proof.
- AVA Web Console opens only when a completed runner result exists with VM name,
  SSH host, SSH port, and no runner error.
- If provisioning is still running, Web Console explains that the VM is not
  ready yet and tells the user to wait for completion.

---

## Resolved Issues (Phase 9.5 Hardening — 2026-05-04)

### Issue 1: ChromaDB Permission Denied Decay

**Symptom:** `ava-agent` crash-loops with `Permission denied (os error 13)` from
ChromaDB's Rust HNSW bindings after a container restart.  `chown -R 999:999`
fixes it temporarily but the fix decays on the next restart.

**Root cause:** The previous `/data` mount used a WSL2 host bind path
(`/home/manoj/ava-data:/data`). That made the container's writeability depend
on WSL/Docker ownership mapping. In the user's real environment, even when the
WSL path appeared as UID/GID 999, the container still failed to create
`/data/logs`, `/data/chromadb`, `/data/tmp`, and other runtime directories.

**Fix:** `docker-compose.yml` now uses the Docker named volume `ava_data:/data`
instead of the WSL bind mount. Docker initializes the named volume from the
image's `/data` mount point, preserving stable ownership for the `ava` user
across Windows, WSL, and Docker restarts.

`scripts/docker-entrypoint.sh` remains in place as a lightweight guard:
- Sets `umask 002` so runtime-created files inherit group-write
- Creates all known `/data` subdirectories with `mkdir -p`
- Runs `chmod -R ug+rwX /data` inside the named volume
- Execs `gunicorn` (the CMD)

**Migration:** The old WSL directory is not deleted. Use
`scripts/migrate-ava-data-to-volume.ps1` to preview and, only with `-Execute`
plus an explicit `YES`, copy legacy data into the named volume.

**Verification target:** 3 consecutive `docker restart ava-agent` with health
check after each.

---

### Issue 2: Ollama WSL2 IP Changes on Restart

**Symptom:** `OLLAMA_HOST` was hardcoded to `http://172.24.212.81:11434` in
`docker-compose.yml`.  After a WSL restart the IP changes and AVA can no
longer reach Ollama (`ollama: false` in health check).

**Fix:**
- `scripts/sync-ollama-host.ps1` — runs `wsl hostname -I`, parses the first
  IP, writes `OLLAMA_HOST=http://<ip>:11434` to `.env`.
- `docker-compose.yml` changed to `${OLLAMA_HOST:-http://172.24.212.81:11434}`
  (variable substitution reads from `.env`; falls back to the old IP if sync
  hasn't run yet).
- `scripts/start-ava.ps1` calls `sync-ollama-host.ps1` before `docker compose
  up`, so the IP is always current on startup.
- `/health` now checks the same `OLLAMA_HOST` value instead of hardcoding
  `host.docker.internal`, so health reflects the actual runtime route.

---

### Issue 3: Runner Requires Manual PowerShell Start

**Symptom:** User had to manually run `.\scripts\start_host_runner.ps1` after
every login or AVA restart.

**Fix:**
- `scripts/install-runner-task.ps1` — creates `AVA Host Runner.cmd` in the
  current user's Windows Startup folder. The hook starts `start-ava.ps1`, not
  the runner directly, so Docker/Redis/AVA are brought up before the runner
  tries to connect.
- `scripts/start-ava.ps1` also starts the runner in step 5, so it comes up with
  the rest of the stack even if the login hook has not fired yet.
- AVA chat now checks the runner heartbeat before approval. If the runner is
  offline, AVA tells the user that provisioning cannot start yet instead of
  silently queuing a job.

**One-time setup:** `.\scripts\install-runner-task.ps1`

---

### Issue 4: Stale Runner Process Holds Old Code in Memory

**Symptom:** After a code update, `host_runner.py` still ran the old version
because the long-lived Python process cached it.  Fix was `Stop-Process python`
manually.

**Fix:**
1. `start_host_runner.ps1` now defaults to `$MaxJobs = 0`, which means the
   product runner stays alive and can process every approved AVA chat job.
2. `start-ava.ps1` and `install-runner-task.ps1` explicitly launch the runner
   with `-MaxJobs 0` so approved jobs do not remain stuck in `queued` after the
   first provisioning request.
3. For development-only one-job runs, call `.\scripts\start_host_runner.ps1 -MaxJobs 1`.

**Follow-up correction on 2026-05-05:** The earlier one-shot default was useful
for fresh-code debugging, but it was wrong for product behavior. It caused AVA
to queue later approved jobs while no runner was alive to execute them.

---

### Issue 5: Stale `seed.iso` Files Accumulate

**Symptom:** The Phase 9 fix (commit `9946575`) correctly prevents destroying a
working VM when VBoxSVC holds the file lock on `seed.iso`, but leaves the file
on disk.  Multiple runs accumulate stale ISOs.

**Fix:**
- `scripts/cleanup-stale-seeds.ps1` — finds `.ava-runner/*/seed.iso` files
  older than 1 hour and removes them, silently skipping any still locked by
  VBoxSVC.
- `scripts/install-runner-task.ps1` also registers "AVA Cleanup Stale Seeds"
  as a daily Scheduled Task at 03:00.

---

### Issue 6: Docker Desktop Crashes Leave AVA Unrecoverable

**Symptom:** Docker Desktop died 5 times during Phase 9 work, requiring manual
restart and multi-step AVA recovery.

**Fix:**
- `scripts/start-ava.ps1` starts with a Docker health check (`docker info`).
  If Docker is not reachable after 3 attempts (30 s each), it calls
  `Start-Process` on `Docker Desktop.exe` and waits up to 90 s more before
  giving up with a clear error message.
- The same script handles all downstream steps, so recovery from a Docker crash
  is always: close the crashed state, run `.\scripts\start-ava.ps1`.

---

## Implementation And Validation Log (2026-05-03 to 2026-05-06)

This section records the Phase 9 implementation work after the initial runner
bridge design. It is intentionally factual: failures are listed alongside the
fixes because they shaped the final product behavior.

### 2026-05-03 — First Live Chat-to-VM Success

What worked:

- AVA chat accepted a web-server request, collected specs, gated approval, and
  queued a host-runner job.
- The Windows-native runner picked up the Redis job, cloned the Ubuntu template,
  injected cloud-init access, verified SSH, installed nginx, applied
  `baseline_linux`, and verified HTTP 200.
- The VM was reachable through NAT forwarding using PuTTY/SSH on localhost and
  a host HTTP URL.

What failed or felt wrong:

- Chat state and runner state were out of sync. The runner had completed
  bootstrap/hardening/verification, but chat still asked the user to confirm
  first login and hardening.
- Cleanup of cloud-init seed media was fragile because VirtualBox held locks on
  attached `seed.iso` files.

Fixes that followed:

- Seed cleanup was made non-destructive: cleanup races must not destroy a
  verified VM.
- Runner evidence became the source of truth for status/evidence/verify prompts.

### 2026-05-04 — Operational Hardening

What failed:

- `ava-agent` repeatedly crash-looped with ChromaDB permission errors after
  restarts.
- Manual startup required several steps: Docker, WSL/Ollama IP, AVA containers,
  and the host runner.
- Runner startup through Windows automation was not reliable enough for a
  product workflow.

Fixes:

- Moved AVA runtime data to the Docker named volume `ava_data`.
- Added the Docker entrypoint guard for `/data` directory creation and
  permissions.
- Added `start-ava.ps1` as the one-command startup orchestrator.
- Added WSL Ollama IP sync into `.env`.
- Added startup-folder runner installation and daily stale seed cleanup support.
- Added runner heartbeat preflight so AVA refuses provisioning start when the
  host runner is not healthy.

Product outcome:

- After reboot, the intended user workflow is to run `.\scripts\start-ava.ps1`
  or rely on the login startup hook installed by `install-runner-task.ps1`.
- AVA should record approval as consent, but should not issue a temporary
  password or queue a VM if the runner is offline.

### 2026-05-05 — Connection Reporting And Repeatability Polish

What failed:

- Multiple VMs caused NAT port collisions.
- PuTTY connection details were not always visible at the right time.
- Temporary passwords contained characters that were easy to misread or hard to
  type into PuTTY.
- Some completed-runner responses still sounded like the VM was pending.

Fixes:

- VirtualBox NAT port allocation now avoids collisions.
- AVA reports PuTTY-friendly connection details after the runner writes the
  result: VM name, `127.0.0.1`, SSH port, username, and web URL.
- Temporary passwords were changed to be PuTTY-friendly while still one-time.
- Completed runner jobs now drive the status/evidence/verification response even
  if the transient Redis status key expires.

Product outcome:

- Users can ask `how do I connect with PuTTY?` after completion and receive the
  correct Host Name, Port, Connection Type, username, and web URL.
- AVA does not reprint temporary passwords after the approval response.

### 2026-05-06 — Final Chat UX And Evidence Polish

What failed:

- The web UI could collapse into a narrow column when long verification evidence
  was rendered.
- Misspelled status prompts such as `provisionning status` could route through
  the wrong path.
- After completion, `I logged in and changed the password` and `yes harden it`
  could fall through to the old generic v1 scope response.
- `.ava-runner/` and `.claude/` appeared as untracked local files even though
  they are runtime/local artifacts.

Fixes:

- The AVA shell layout was forced to full viewport width with stable sidebar and
  main-pane sizing.
- Status intent matching now handles common provisioning/status wording variants
  and the `provisionning` typo.
- Completed provisioning sessions now absorb late first-login and hardening
  follow-ups and respond with "already done" style product truth.
- `.ava-runner/` and `.claude/` are ignored in Git because they may contain
  runner logs, seed ISO artifacts, temporary key material, or local CLI settings.

Product outcome:

- Long verification evidence no longer breaks the page layout.
- Completed VM sessions feel consistent: status, evidence, verify, PuTTY, late
  login confirmation, and late hardening confirmation all report the same
  runner-backed truth.

---

## Remaining Known Issues

### Runner orphan on mid-job crash

If `host_runner.py` crashes while a job is at `picked_up` or beyond, the job
is orphaned.  Status stays at the last written phase and the chat session shows
stale state.

**Workaround (v2.0.0):** Delete the Redis key manually:
```
docker exec agent_redis redis-cli DEL ava:provisioning:jobs:status:<job_id>
```
**Planned fix:** v2.1 — crash-recovery scan on runner start.

### Redis disconnection exits the runner

If Redis becomes unreachable mid-poll, the runner exits cleanly. Restart AVA
with `.\scripts\start-ava.ps1`; the chat approval preflight will block new VM
requests until the runner heartbeat is healthy again.

### Ollama shown as `false` in health check after WSL restart

Expected until `start-ava.ps1` is run (which calls `sync-ollama-host.ps1`).
Not a bug — Ollama itself is healthy; AVA just has the wrong IP in memory until
the containers are restarted with the synced `.env`.

### Startup automation depends on Windows environment policy

`install-runner-task.ps1` installs a current-user startup hook and attempts to
create a daily cleanup task where Windows permits it. On machines with stricter
execution policy or Task Scheduler restrictions, the fallback is still explicit
and product-safe: run `.\scripts\start-ava.ps1` after login. AVA will refuse VM
approval if the runner heartbeat is missing, so this fails safely instead of
silently creating a dead queued job.

### Browser login/session is still local-dev style

AVA runs as a local HTTPS service at `https://localhost:5443`. Docker Desktop
must be available locally, and the browser session depends on the local AVA
login state. This is acceptable for v2.0.0 local product validation. A packaged
installer or tray app is a future productization step.

---

## New File Inventory (Phase 9.5)

| File | Purpose |
|------|---------|
| `scripts/docker-entrypoint.sh` | Container entrypoint — prepares `/data` directories before gunicorn |
| `scripts/check-ava-storage.ps1` | Non-destructive storage diagnostic |
| `scripts/migrate-ava-data-to-volume.ps1` | Optional dry-run-first migration from WSL bind path to Docker volume |
| `scripts/sync-ollama-host.ps1` | Detects WSL2 IP, writes `OLLAMA_HOST` to `.env` |
| `scripts/start-ava.ps1` | One-command full startup orchestrator |
| `scripts/install-runner-task.ps1` | Installs current-user Startup hook for `start-ava.ps1` and daily cleanup task where allowed |
| `scripts/cleanup-stale-seeds.ps1` | Removes stale `seed.iso` files older than 1 hour |

Modified files: `Dockerfile`, `docker-compose.yml`, `web_agent_v2.1_guardrail.py`,
`scripts/start-ava.ps1`, `scripts/start_host_runner.ps1`

---

## Exit Criteria (Phase 9 — live product validation)

- Approving a provisioning request from chat records user consent without
  creating a VM yet
- `continue provisioning` triggers VM creation on Windows when the host runner
  heartbeat is healthy
- AVA refuses provisioning start safely when the host runner is not healthy
- AVA chat session attaches the real `instance_id` after VM creation
- AVA provides SSH/PuTTY connection details: host, port, username, VM name
- AVA shows live runner progress, expected-time guidance, and clear next steps
  while the VM is being created
- nginx is bootstrapped on the created VM
- `baseline_linux` hardening is applied by the runner by default
- HTTP 200 is verified from both guest and host perspectives
- Status/evidence/verify/PuTTY prompts report real runner evidence
- AVA Web Console remains blocked until completed runner evidence exists
- Late first-login and hardening follow-ups after completion do not fall through
  to the generic scope response
- UI remains usable when long verification evidence is displayed

Phase 9.5 adds: startup and recovery are product-safe. The preferred path is
`.\scripts\start-ava.ps1` or the login startup hook installed by
`.\scripts\install-runner-task.ps1`; if the runner is not alive, AVA blocks
provisioning start rather than issuing credentials for a job that cannot
execute.

---

## Next: Phase 9 Day-2 Operations (`v2.1`)

Phase 9 does not stop at VM creation. The next product chapter is **Phase 9
Day-2 Operations**, documented in
`docs/AVA_V2_PHASE9_DAY2_OPERATIONS.md`.

The goal is simple: AVA created the server, so AVA should also manage it.

Initial v2.1 focus:

- show VM/web-server status
- verify the web server
- show nginx logs
- restart nginx with approval
- take VirtualBox snapshots with approval
- roll back to the latest AVA snapshot with approval
- stop/start the VM with approval
- return evidence after every operation

This comes before PostgreSQL or multi-VM work because every future server role
will need the same Day-2 management layer.

---

## Current Product Contract Addendum (2026-06-15)

This addendum records the later Phase 9 reliability and UX work that happened
after the original v2.0.0/v2.0.1 milestone notes. It is intentionally additive:
older sections above remain as historical context.

### Provisioning conversation contract

The active provisioning flow is now deliberately two-step:

1. AVA creates a plan and prints the exact approval phrase:
   `approve <approval_id>`.
2. Approval records consent only. No VM is created and no password is issued at
   this point.
3. AVA then asks the user to reply `continue provisioning`.
4. `continue provisioning` checks that the Windows host runner heartbeat is
   healthy.
5. Only after the runner is healthy does AVA issue the one-time temporary
   password, queue the runner job, and report that provisioning is in progress.
6. A lightweight background watcher posts a final completed/failed message when
   the runner writes the result.

This change prevents the old failure mode where AVA showed credentials and a
queued job even though no host runner was available to create the VM.

### Live progress and readiness

Status responses now expose the runner's current stage when provisioning is
still active. Examples include `queued`, `provisioning`, `ssh_ready`,
`bootstrapping`, `completed`, and `failed`.

While a VM is visible but not fully ready, AVA may show provisional connection
details from runner progress, but Web Console remains gated until completed
runner evidence exists. The boundary is:

- visible VM is not enough
- SSH TCP reachable is not enough
- completed bootstrap, hardening, and HTTP verification are required before AVA
  treats the server as ready

### Duplicate and stale server safety

Phase 9 now protects against common demo and operator mistakes:

- `i want a web server` will not create a second AVA-managed web server by
  accident if a completed web server is already attached.
- `create another web server` is treated as an intentional provisioning request,
  not as a generic DevOps question.
- Requested hostnames are checked before execution. If an AVA-managed completed
  server already uses the hostname, AVA blocks the duplicate before issuing
  credentials or queueing the runner.
- The Windows host runner also publishes registered VirtualBox VM names in its
  heartbeat, so AVA can block a duplicate hostname even when the VM exists in
  local infrastructure but is not attached to the current AVA session.
- The host runner heartbeat now includes VirtualBox inventory details for
  registered VM names, including power state and provider status when available.
  This lets AVA distinguish running, powered-off, saved, deleted, and stale
  server states before presenting Day-2 actions.
- Manual VirtualBox deletion is reconciled through live runner truth instead of
  trusting old stored evidence forever.
- Failed or expired runner state is reflected back into the provisioning
  session so stale Redis TTL expiry does not permanently block new requests.

### Multi-server operation boundary

AVA can now work with more than one AVA-created server in a local VirtualBox
environment. The current supported pattern is name-based targeting:

- `list my servers`
- `show offline servers`
- `verify ava-web-03`
- `show nginx logs for ava-web-03`
- `open web console for ava-web-03`
- `restart nginx on ava-web-03`
- `stop ava-web-03`
- `start ava-web-03`
- `delete ava-web-03`

The Web Console opens the latest completed VM by default only when that is
unambiguous. When more than one server exists, the product-safe instruction is
to name the target server.

For lifecycle commands, AVA should require an exact VM hostname before queuing
approval. If the user says `start server`, `stop server`, or `delete server`,
AVA should list available AVA-managed targets with their current power state and
ask for a precise command such as `start ava-web-03`. This avoids accidental
power or deletion actions against the wrong VM.

### Current known boundaries

- The current role is still `web_server` on Ubuntu/VirtualBox.
- The browser Web Console is for AVA-managed VMs, not arbitrary public SSH
  targets.
- Manual public-IP SSH console support is a future feature and must include
  allowlist policy, approval, host-key warnings, no password persistence, and
  audit records.
- Full PuTTY-speed terminal behavior needs the future xterm/WebSocket transport.
  The current console is usable for common commands but remains an intermediate
  browser terminal.
