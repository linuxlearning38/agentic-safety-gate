# AVA Operator Guide

> This guide is for anyone running AVA day-to-day who did not build it.
> You need: Windows 11, WSL2, Docker Desktop, VirtualBox, PowerShell 5.1+.

---

## First-Time Setup (run once)

**1. Clone the repo and navigate to it in PowerShell:**
```powershell
cd C:\path\to\devops-agent
```

**2. Copy the example env file and fill in your secrets:**
```powershell
Copy-Item .env.example .env   # if it exists; otherwise .env is already present
```

**3. Check AVA storage:**
```powershell
.\scripts\check-ava-storage.ps1
```

This is read-only. It confirms AVA is using the Docker named volume `ava_data`
for `/data`, which avoids the old WSL bind-mount permission decay.

Optional legacy-data migration dry run:
```powershell
.\scripts\migrate-ava-data-to-volume.ps1
```

This does not copy or delete anything unless rerun with `-Execute` and explicit
confirmation.

**4. Install startup hooks (so AVA and the runner start automatically at login):**
```powershell
.\scripts\install-runner-task.ps1
```
This installs a delayed Windows logon startup path for AVA. The delay is
intentional: after reboot, Docker Desktop and WSL2 can need a short warm-up
before Redis is stable enough for the host runner heartbeat.

Verify the startup hooks anytime:
```powershell
.\scripts\check-ava-autostart.ps1
```

**5. Start AVA for the first time:**
```powershell
.\scripts\start-ava.ps1
```

Done.  Open `https://localhost:5443` in your browser.

---

## Daily Startup

After a reboot or after Docker Desktop crashes, run:
```powershell
.\scripts\start-ava.ps1
```

This handles everything:
- Starts Docker Desktop if it is not running
- Detects the current WSL2 IP and updates the Ollama connection
- Brings up all containers
- Waits for the health check to pass
- Starts the VM provisioning runner in the background
- Lets AVA verify the runner heartbeat before accepting VM approvals

If you installed the startup hooks, Windows should run this automatically after
login. If AVA opens but provisioning says the runner is unhealthy, run the
read-only checker first:
```powershell
.\scripts\check-ava-autostart.ps1
```

---

## Stopping AVA

```powershell
docker compose down
```

This stops all containers but preserves data in the Docker volume `ava_data`.

---

## Health Check

```
https://localhost:5443/health
```

Expected response:
```json
{"status": "ok", "dependencies": {"redis": true, "opa": true, "ollama": true}}
```

`ollama: false` means the WSL2 IP changed.  Fix: run `.\scripts\start-ava.ps1`
again (it will sync the IP and restart the containers).

---

## Provisioning a VM

1. Open `https://localhost:5443` and log in.
2. Type a request in the chat, for example:
   ```
   provision a web server with nginx
   ```
3. AVA will present a plan.  Review it and type `approve`.
4. The VM is created in VirtualBox (typically 5-10 minutes).
5. Ask AVA for status:
   ```
   show provisioning status
   ```
6. When complete, AVA reports the SSH details and HTTP verification result.

---

## Troubleshooting

### Container keeps restarting
```powershell
docker logs ava-agent --tail 50
.\scripts\check-ava-storage.ps1
```
Most common causes:
- `/data` permissions: should be stable through Docker named volume `ava_data`
- Redis not started: `docker compose up -d` again
- Cert files missing: ensure `certs/ava.crt` and `certs/ava.key` exist

### Ollama shows `false` in health
The WSL2 IP changed.  Run `.\scripts\start-ava.ps1` to sync and restart.

### VM provisioning jobs do not run
The host runner is probably not started. AVA checks the runner heartbeat before
approval; if the runner is missing, AVA should refuse to issue credentials or
queue a VM job.

Check the Windows host runner process:
```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'provisioning.runner.host_runner|host_runner.py' } |
  Select-Object ProcessId,CommandLine
```
If nothing appears, start the full AVA stack:
```powershell
.\scripts\start-ava.ps1
```

### seed.iso files piling up in `.ava-runner/`
Run the cleanup manually:
```powershell
.\scripts\cleanup-stale-seeds.ps1
```
Or wait for the scheduled daily run at 03:00.

### Docker Desktop crashed
Run `.\scripts\start-ava.ps1` — it will detect and restart Docker Desktop.
It also verifies the `ava_data` Docker volume exists before starting AVA. If
legacy WSL data is present and the volume is missing, it stops and asks you to
run the migration script instead of silently starting with empty state.

---

## Log Locations

| What | Where |
|------|-------|
| AVA container logs | `docker logs ava-agent` |
| Host runner log | `.ava-runner\host_runner.log` |
| Gunicorn access log | Inside container at `/tmp/ava_access.log` |
| Gunicorn error log | Inside container at `/tmp/ava_error.log` |

---

## Scripts Reference

| Script | When to run |
|--------|-------------|
| `start-ava.ps1` | Every startup / after reboot |
| `install-runner-task.ps1` | Once, on first setup |
| `check-ava-autostart.ps1` | Read-only check for startup task, AVA health, and runner heartbeat |
| `check-ava-storage.ps1` | Read-only storage and volume diagnostic |
| `migrate-ava-data-to-volume.ps1` | Optional legacy data migration; dry-run by default |
| `sync-ollama-host.ps1` | If Ollama is unreachable after WSL restart |
| `cleanup-stale-seeds.ps1` | If `.ava-runner/` has stale seed.iso files |
| `start_host_runner.ps1` | If runner is not running (normally automatic) |

---

## Current Provisioning Flow (2026-06-15)

The current AVA flow separates approval from execution so the product does not
create VMs before the user is ready.

1. Ask AVA for a server:
   ```
   i want a web server
   ```
2. Provide specs:
   ```
   2 CPU, 4 GB RAM, 30 GB disk, hostname ava-web-03
   ```
3. AVA prints an approval ID and the exact approval phrase:
   ```
   approve <approval_id>
   ```
4. After approval, AVA records consent only and asks for:
   ```
   continue provisioning
   ```
5. AVA checks the Windows host runner, issues the one-time temporary password,
   queues the VirtualBox job, and starts showing progress.
6. Provisioning usually takes 3-8 minutes depending on VirtualBox boot time and
   Ubuntu cloud-init.
7. AVA posts a completed or failed result automatically when the runner writes
   the final status. You can also ask:
   ```
   provisioning status
   ```
8. When complete, AVA shows the VM name, SSH/PuTTY port, username, and web URL.
9. To access the server from the browser, type:
   ```
   open web console
   ```
   If more than one AVA-managed server exists, name the target:
   ```
   open web console for ava-web-03
   ```

### Demo-safe behavior

- If a completed AVA-managed web server already exists, AVA will not create a
  second one from a generic `i want a web server` prompt.
- To intentionally create another server, say `create another web server` and
  include specs.
- Do not reuse an existing AVA-managed hostname. AVA should block duplicate
  hostnames before credentials are issued.
- To review existing AVA-managed servers, ask:
  ```
  list my servers
  ```
- To find servers that are powered off, saved, or otherwise not ready, ask:
  ```
  show offline servers
  ```
- AVA reports live VirtualBox power state when the host runner is online. If a
  server is offline, AVA should guide the operator to use exact hostname
  commands such as:
  ```
  start ava-web-03
  stop ava-web-03
  delete ava-web-03
  ```
- If the operator asks `start server`, `stop server`, or `delete server`
  without a hostname, AVA should ask which VM to target. AVA should not guess.
- Prefer deleting AVA-created VMs through AVA when that flow is available. If
  you manually delete a VM in VirtualBox, ask AVA to verify or start a fresh
  provisioning request so stale stored history can be reconciled.

### Runner startup warning

If startup prints:

```
WARNING: Host runner did not publish a heartbeat within 15s.
```

AVA may still load in the browser, but provisioning and Web Console operations
will not work until the Windows host runner is online. First run the read-only
startup checker:

```powershell
.\scripts\check-ava-autostart.ps1
```

If the scheduled task or Startup-folder fallback is missing, reinstall the
startup hooks:

```powershell
.\scripts\install-runner-task.ps1
```

If the hooks exist but the heartbeat is still missing, check the startup log
files shown by the script, then run:

```powershell
.\scripts\start-ava.ps1
```

If needed, start the runner directly:

```powershell
.\scripts\start_host_runner.ps1
```

The safe product behavior is: no runner heartbeat means no VM provisioning.
