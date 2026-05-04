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

**3. Register Scheduled Tasks (so the runner starts automatically at login):**
```powershell
.\scripts\install-runner-task.ps1
```

**4. Start AVA for the first time:**
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

---

## Stopping AVA

```powershell
docker compose down
```

This stops all containers but preserves data in `/home/manoj/ava-data`.

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
```
Most common causes:
- `/data` permissions: already fixed by the entrypoint script in v2.0.0+
- Redis not started: `docker compose up -d` again
- Cert files missing: ensure `certs/ava.crt` and `certs/ava.key` exist

### Ollama shows `false` in health
The WSL2 IP changed.  Run `.\scripts\start-ava.ps1` to sync and restart.

### VM provisioning jobs do not run
The host runner is not started.  Check:
```powershell
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'host_runner' }
```
If nothing appears, start it:
```powershell
.\scripts\start_host_runner.ps1
```

### seed.iso files piling up in `.ava-runner/`
Run the cleanup manually:
```powershell
.\scripts\cleanup-stale-seeds.ps1
```
Or wait for the scheduled daily run at 03:00.

### Docker Desktop crashed
Run `.\scripts\start-ava.ps1` — it will detect and restart Docker Desktop.

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
| `sync-ollama-host.ps1` | If Ollama is unreachable after WSL restart |
| `cleanup-stale-seeds.ps1` | If `.ava-runner/` has stale seed.iso files |
| `start_host_runner.ps1` | If runner is not running (normally automatic) |
