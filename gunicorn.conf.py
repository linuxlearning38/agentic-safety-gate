import os
# gunicorn.conf.py
# AVA Phase 4 Day 9 — Production WSGI config

import multiprocessing

# ── Binding ───────────────────────────────────────────────────────────────────
# HTTP  on 5002 (keep for local dev / curl testing)
# HTTPS on 5443 (production)
# Gunicorn doesn't natively bind two ports — start_ava.sh handles both

# ── Workers ───────────────────────────────────────────────────────────────────
# 2 workers on Ryzen 1600 + 24GB WSL2
# Rule of thumb: (2 x CPU cores) + 1 — but AVA is GPU-bound, 2 is enough
workers         = 2
worker_class    = "sync"          # sync is correct — AVA uses Ollama (external)
threads         = 1               # no threading — Ollama calls are blocking
timeout         = 600             # 5 min — ReAct loops can take 2-3 min
keepalive       = 5

# ── Logging ───────────────────────────────────────────────────────────────────
accesslog       = os.getenv("GUNICORN_ACCESS_LOG", "/mnt/i/ai-lab/logs/ava_access.log")
errorlog        = os.getenv("GUNICORN_ERROR_LOG",  "/mnt/i/ai-lab/logs/ava_error.log")
loglevel        = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sμs'

# ── Process ───────────────────────────────────────────────────────────────────
pidfile         = "/tmp/ava_gunicorn.pid"
daemon          = False           # systemd/start_ava.sh manages process
preload_app     = False            # load app once, fork workers (saves RAM)

# ── Security ──────────────────────────────────────────────────────────────────
limit_request_line    = 4096
limit_request_fields  = 100
limit_request_field_size = 8190
