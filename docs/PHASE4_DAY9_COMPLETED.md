# AVA — Phase 4 Day 9: Gunicorn + HTTPS

**Date:** April 2, 2026  
**Engineer:** Manoj — Senior DevOps Engineer, Delhi  
**Repo:** https://github.com/linuxlearning38/agentic-safety-gate (private, master branch)  
**Commit:** `pending`

---

## What Was Built

Replaced Flask dev server with Gunicorn (production WSGI). Added self-signed TLS. Single `start_ava.sh` script manages start/stop/restart/logs. AVA now runs on HTTPS :5443 (production) and HTTP :5002 (local dev).

---

## Files Created

```
/mnt/i/ai-lab/projects/devops-agent/
├── wsgi.py               ← Gunicorn entrypoint
├── gunicorn.conf.py      ← WSGI config (workers, timeout, logging)
├── start_ava.sh          ← Start/stop/restart/status/logs script
├── certs/
│   ├── ava.crt           ← Self-signed TLS cert (RSA 4096, 365 days)
│   └── ava.key           ← Private key (chmod 600)
├── patch_day9.py         ← Patcher script
└── test_day9.py          ← Test suite
```

---

## Why Gunicorn

| Flask Dev Server | Gunicorn |
|---|---|
| Single-threaded | 2 workers (parallel requests) |
| No TLS | Self-signed TLS on :5443 |
| Crashes drop all connections | Workers restart independently |
| `WARNING: Do not use in production` | Production-grade WSGI |
| No access logs | Structured access + error logs |

---

## Start / Stop Commands

```bash
cd /mnt/i/ai-lab/projects/devops-agent/

./start_ava.sh            # start HTTPS :5443 + HTTP :5002
./start_ava.sh stop       # stop all
./start_ava.sh restart    # stop + start
./start_ava.sh status     # check ports
./start_ava.sh logs       # tail logs
```

**Expected output on start:**
```
── Starting AVA ────────────────────────────────────
  Starting HTTPS on :5443...
  Starting HTTP  on :5002...

  ✅  AVA running
  HTTPS → https://localhost:5443
  HTTP  → http://localhost:5002
  Logs  → /mnt/i/ai-lab/logs/
```

---

## Gunicorn Config

```python
# gunicorn.conf.py
workers      = 2          # 2 sync workers — GPU-bound, more doesn't help
worker_class = "sync"     # sync is correct for Ollama (blocking external calls)
timeout      = 300        # 5 min — ReAct loops need this
preload_app  = True       # load app once, fork — saves ~500MB RAM
```

**Why 2 workers, not more:**  
AVA is GPU-bound. When Qwen 2.5 14B is running inference, it saturates the RTX 5060 Ti. More workers would queue anyway. 2 workers = 1 active + 1 ready.

**Why sync, not gthread/gevent:**  
Ollama calls are blocking subprocess calls. Async workers (gevent) give no benefit and add complexity.

---

## HTTPS Details

```
Certificate: RSA 4096-bit, self-signed
Duration:    365 days
Subject:     CN=localhost, O=AVA-SecDevOps, C=IN
SAN:         DNS:localhost, IP:127.0.0.1
```

Self-signed = browsers show warning. Use `-k` or `--insecure` with curl. For real deployment, replace with Let's Encrypt cert (Day 10 Docker makes this easier).

---

## wsgi.py Design

Flask app filename has a dot (`web_agent_v2.1_guardrail.py`) — Python can't import it with normal `import` statements. `wsgi.py` uses `importlib.util.spec_from_file_location()` to load it by path.

```python
# wsgi.py loads the app like this:
_spec = importlib.util.spec_from_file_location('web_agent', 'web_agent_v2.1_guardrail.py')
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
application = _mod.app   # Gunicorn looks for `application`
```

---

## Log Files

```
/mnt/i/ai-lab/logs/
├── ava_access.log        ← HTTPS requests (format: IP method path status μs)
├── ava_access_http.log   ← HTTP requests
└── ava_error.log         ← Errors + startup logs (both HTTP + HTTPS)
```

Tail logs:
```bash
./start_ava.sh logs
# OR
tail -f /mnt/i/ai-lab/logs/ava_error.log
```

---

## Testing

```bash
# Get token over HTTPS (-k = skip cert verify for self-signed)
TOKEN=$(curl -sk https://localhost:5443/auth/login \
  -X POST -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<YOUR_ADMIN_PASSWORD>"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Test suite
python3 test_day9.py --token $TOKEN

# Manual checks
curl -sk https://localhost:5443/scan/check \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

curl -sk https://localhost:5443/stats \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

---

## Known Issues & Notes

| Issue | Notes |
|---|---|
| Self-signed cert browser warning | Expected. Use `-k` in curl. Replace with Let's Encrypt in production. |
| Lynis OOM on 16GB WSL2 | Fixed — WSL2 now set to 24GB via `.wslconfig` |
| Rate limit resets on restart | In-memory storage. Redis upgrade in Phase 4.5. |

---

## Remaining Phase 4 Work

| Day | Task | Status |
|---|---|---|
| Day 8 | Trivy + Lynis | ✅ `a8042c8` |
| Day 9 | Gunicorn + HTTPS | ✅ |
| Day 10 | Docker Containerization | ⬜ |

---

## Git Commit

```bash
git add wsgi.py gunicorn.conf.py start_ava.sh certs/ \
        patch_day9.py test_day9.py PHASE4_DAY9_COMPLETED.md
git commit -m "Day 9: Gunicorn + HTTPS production server

- Gunicorn 2 workers, sync, 300s timeout
- Self-signed TLS on :5443 (RSA 4096, 365 days)
- HTTP :5002 kept for local dev
- start_ava.sh: start/stop/restart/status/logs
- wsgi.py: importlib loader for dotted filename
- Logs: /mnt/i/ai-lab/logs/"
```

---

*AVA — Built by Manoj | Powered by Qwen 2.5 14B + ChromaDB + Ollama*  
*Phase 4 Day 9 Complete | April 2, 2026*
