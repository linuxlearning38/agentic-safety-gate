# AVA v2 Phase 9 — Chat-to-VM Provisioning: Operations Guide

Branch: `v2-development`
Status: Functional — Phase 9.5 operational hardening complete (2026-05-04)

## Operator Quickstart

**One-time setup (run once per Windows user account):**
```powershell
.\scripts\install-runner-task.ps1
```
This registers two Windows Scheduled Tasks: the host runner at login and a
daily cleanup job at 03:00.

**Daily startup (or after any reboot):**
```powershell
.\scripts\start-ava.ps1
```
That is the only command needed.  It:
1. Detects and waits for Docker; starts Docker Desktop if needed
2. Auto-detects the current WSL2 IP and writes `OLLAMA_HOST` to `.env`
3. Brings up all containers with `docker compose up -d`
4. Polls `https://localhost:5443/health` until healthy (or 120 s timeout)
5. Starts the host runner in a background minimised window

**Verify everything is up:**
```powershell
docker ps                               # all containers healthy
curl -k https://localhost:5443/health   # {"status":"ok",...}
```

---

## Architecture

```
User browser
     |
     v
AVA chat (/ask)             [Docker container ava-agent]
provisioning/serving.py
     |  enqueue job after approval
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

## Resolved Issues (Phase 9.5 Hardening — 2026-05-04)

### Issue 1: ChromaDB Permission Denied Decay

**Symptom:** `ava-agent` crash-loops with `Permission denied (os error 13)` from
ChromaDB's Rust HNSW bindings after a container restart.  `chown -R 999:999`
fixes it temporarily but the fix decays on the next restart.

**Root cause:** The container's `/data` bind mount (WSL2 `ext4` path
`/home/manoj/ava-data`) can develop mode-bit drift when runtime processes
(SQLite WAL writer, ChromaDB HNSW index builder, trivy) create new
subdirectories without a controlled umask.  On restart a subsequent process
gets `EACCES` opening those files for write.

**Fix:** `scripts/docker-entrypoint.sh` — an ENTRYPOINT script baked into the
image that runs as user 999 before gunicorn starts:
- Sets `umask 002` so all runtime-created files inherit group-write
- Creates all known `/data` subdirectories with `mkdir -p`
- Runs `chmod -R ug+rwX /data` to correct any existing drift
- Execs `gunicorn` (the CMD) with no overhead beyond the chmod traverse

Dockerfile change: added `ENTRYPOINT ["/app/entrypoint.sh"]` before `CMD`.

**Verification:** 3 consecutive `docker restart ava-agent` with health check
after each — all passed on 2026-05-04.

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

---

### Issue 3: Runner Requires Manual PowerShell Start

**Symptom:** User had to manually run `.\scripts\start_host_runner.ps1` after
every login or AVA restart.

**Fix:**
- `scripts/install-runner-task.ps1` — registers "AVA Host Runner" as a Windows
  Scheduled Task at user login, with automatic restart (up to 3 times with a
  5-minute delay) if the process exits.
- `scripts/start-ava.ps1` also starts the runner in step 5, so it comes up
  with the rest of the stack even if the Scheduled Task hasn't fired yet.

**One-time setup:** `.\scripts\install-runner-task.ps1`

---

### Issue 4: Stale Runner Process Holds Old Code in Memory

**Symptom:** After a code update, `host_runner.py` still ran the old version
because the long-lived Python process cached it.  Fix was `Stop-Process python`
manually.

**Fix (2-line change to `start_host_runner.ps1`):**
1. Added `$MaxJobs = 1` as default — the runner processes one job and exits,
   so the next job launch always starts a fresh Python process with fresh code.
2. Added a kill of any existing `host_runner` Python process before starting,
   preventing two concurrent runners during rapid restarts.

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

If Redis becomes unreachable mid-poll, the runner exits cleanly.  It will
restart automatically via the Scheduled Task (up to 3 times, 5-minute delay).

### Ollama shown as `false` in health check after WSL restart

Expected until `start-ava.ps1` is run (which calls `sync-ollama-host.ps1`).
Not a bug — Ollama itself is healthy; AVA just has the wrong IP in memory until
the containers are restarted with the synced `.env`.

---

## New File Inventory (Phase 9.5)

| File | Purpose |
|------|---------|
| `scripts/docker-entrypoint.sh` | Container entrypoint — fixes `/data` permissions before gunicorn |
| `scripts/sync-ollama-host.ps1` | Detects WSL2 IP, writes `OLLAMA_HOST` to `.env` |
| `scripts/start-ava.ps1` | One-command full startup orchestrator |
| `scripts/install-runner-task.ps1` | Registers Windows Scheduled Tasks (one-time setup) |
| `scripts/cleanup-stale-seeds.ps1` | Removes stale `seed.iso` files older than 1 hour |

Modified files: `Dockerfile`, `docker-compose.yml`, `scripts/start_host_runner.ps1`

---

## Exit Criteria (Phase 9 — met 2026-05-02)

- Approving a provisioning request from chat triggers VM creation on Windows
  within 30 seconds
- AVA chat session attaches the real `instance_id` after VM creation
- AVA provides SSH connection details (host, port, username)
- nginx is bootstrapped on the created VM
- HTTP 200 verified from host
- `tests/v2_chat_to_vm_e2e_test.py` pass
- Runner survives one `VBoxManage` failure via Phase 7 rollback

Phase 9.5 adds: no manual steps required after reboot.
