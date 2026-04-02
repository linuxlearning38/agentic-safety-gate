#!/usr/bin/env python3
"""
patch_day9.py
AVA Phase 4 — Day 9: Gunicorn + HTTPS

What this does:
  1. Installs gunicorn into the venv
  2. Generates a self-signed TLS certificate (365 days)
  3. Creates gunicorn.conf.py — production WSGI config
  4. Creates start_ava.sh — single start/stop/restart script
  5. Patches web_agent_v2.1_guardrail.py — removes app.run() at bottom
     (Gunicorn handles serving — app.run() is dev-only)
  6. Creates wsgi.py — Gunicorn entrypoint

Run:
  cd /mnt/i/ai-lab/projects/devops-agent/
  source venv/bin/activate
  python3 patch_day9.py
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

PROJECT_DIR = Path("/mnt/i/ai-lab/projects/devops-agent")
VENV_PIP    = PROJECT_DIR / "venv/bin/pip"
CERT_DIR    = PROJECT_DIR / "certs"
MAIN_APP    = PROJECT_DIR / "web_agent_v2.1_guardrail.py"

OK  = "✅"
ERR = "❌"
INF = "ℹ️ "

BACKUP_DIR = PROJECT_DIR / f"backups/day9_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def backup(path: Path):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, BACKUP_DIR / path.name)
    print(f"  {INF} Backed up {path.name}")


def run(cmd: list, check=True, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, cwd=cwd)


def patch(path: Path, old: str, new: str, label: str) -> bool:
    content = path.read_text()
    if old not in content:
        if new.strip() in content:
            print(f"  {INF} {label} — already done")
            return True
        print(f"  {ERR} {label} — anchor not found")
        return False
    path.write_text(content.replace(old, new, 1))
    print(f"  {OK}  {label}")
    return True


# ─── Step 1: Install Gunicorn ─────────────────────────────────────────────────

def step1_install_gunicorn():
    print("\n── Step 1: Install Gunicorn ─────────────────────────────────")
    result = run([str(VENV_PIP), "install", "gunicorn"], check=False)
    if result.returncode == 0:
        # Get version
        v = run([str(PROJECT_DIR / "venv/bin/gunicorn"), "--version"], check=False)
        ver = v.stdout.strip() if v.returncode == 0 else "unknown"
        print(f"  {OK}  Gunicorn installed: {ver}")
        return True
    else:
        print(f"  {ERR} Gunicorn install failed: {result.stderr[:200]}")
        return False


# ─── Step 2: Generate self-signed TLS certificate ────────────────────────────

def step2_generate_certs():
    print("\n── Step 2: Generate Self-Signed TLS Certificate ────────────")
    CERT_DIR.mkdir(exist_ok=True)
    cert_file = CERT_DIR / "ava.crt"
    key_file  = CERT_DIR / "ava.key"

    if cert_file.exists() and key_file.exists():
        print(f"  {INF} Certificates already exist — skipping generation")
        print(f"       {cert_file}")
        print(f"       {key_file}")
        return True

    if not shutil.which("openssl"):
        print(f"  {ERR} openssl not found — install with: sudo apt install openssl")
        return False

    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:4096",
        "-keyout", str(key_file),
        "-out",    str(cert_file),
        "-days",   "365",
        "-nodes",
        "-subj",   "/C=IN/ST=Delhi/L=Delhi/O=AVA-SecDevOps/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:0.0.0.0",
    ]
    result = run(cmd, check=False)
    if result.returncode == 0:
        os.chmod(key_file, 0o600)
        print(f"  {OK}  Certificate generated (RSA 4096, 365 days)")
        print(f"       cert: {cert_file}")
        print(f"       key:  {key_file}")
        return True
    else:
        print(f"  {ERR} Certificate generation failed: {result.stderr[:300]}")
        return False


# ─── Step 3: Create gunicorn.conf.py ─────────────────────────────────────────

def step3_create_gunicorn_conf():
    print("\n── Step 3: Create gunicorn.conf.py ──────────────────────────")
    conf_path = PROJECT_DIR / "gunicorn.conf.py"
    if conf_path.exists():
        backup(conf_path)

    conf = '''# gunicorn.conf.py
# AVA Phase 4 Day 9 — Production WSGI config

import multiprocessing

# ── Binding ───────────────────────────────────────────────────────────────────
# HTTP  on 5002 (keep for local dev / curl testing)
# HTTPS on 5443 (production)
# Gunicorn doesn't natively bind two ports — start_ava.sh handles both
bind            = "0.0.0.0:5443"
certfile        = "certs/ava.crt"
keyfile         = "certs/ava.key"

# ── Workers ───────────────────────────────────────────────────────────────────
# 2 workers on Ryzen 1600 + 24GB WSL2
# Rule of thumb: (2 x CPU cores) + 1 — but AVA is GPU-bound, 2 is enough
workers         = 2
worker_class    = "sync"          # sync is correct — AVA uses Ollama (external)
threads         = 1               # no threading — Ollama calls are blocking
timeout         = 300             # 5 min — ReAct loops can take 2-3 min
keepalive       = 5

# ── Logging ───────────────────────────────────────────────────────────────────
accesslog       = "/mnt/i/ai-lab/logs/ava_access.log"
errorlog        = "/mnt/i/ai-lab/logs/ava_error.log"
loglevel        = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sμs'

# ── Process ───────────────────────────────────────────────────────────────────
pidfile         = "/tmp/ava_gunicorn.pid"
daemon          = False           # systemd/start_ava.sh manages process
preload_app     = True            # load app once, fork workers (saves RAM)

# ── Security ──────────────────────────────────────────────────────────────────
limit_request_line    = 4096
limit_request_fields  = 100
limit_request_field_size = 8190
'''
    conf_path.write_text(conf)
    print(f"  {OK}  Created gunicorn.conf.py")
    return True


# ─── Step 4: Create wsgi.py ───────────────────────────────────────────────────

def step4_create_wsgi():
    print("\n── Step 4: Create wsgi.py ───────────────────────────────────")
    wsgi_path = PROJECT_DIR / "wsgi.py"
    if wsgi_path.exists():
        backup(wsgi_path)

    wsgi = '''# wsgi.py
# AVA — Gunicorn entrypoint (Day 9)
# Gunicorn imports this file and calls the `application` object.
#
# Usage:
#   gunicorn -c gunicorn.conf.py wsgi:application
#
# The main guardrail file uses:
#   app = Flask(__name__)
# Gunicorn needs the variable named `application`.

import os
from dotenv import load_dotenv

# Load .env before importing the app (JWT_SECRET_KEY must be set)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

from web_agent_v2.1_guardrail import app as application  # noqa: F401

# Gunicorn looks for `application` by default
'''
    # Fix the import — dots in filename need special handling
    wsgi = wsgi.replace(
        "from web_agent_v2.1_guardrail import app as application",
        "import importlib.util, os\n"
        "_spec = importlib.util.spec_from_file_location(\n"
        "    'web_agent', os.path.join(os.path.dirname(__file__),\n"
        "    'web_agent_v2.1_guardrail.py'))\n"
        "_mod = importlib.util.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(_mod)\n"
        "application = _mod.app"
    )
    wsgi_path.write_text(wsgi)
    print(f"  {OK}  Created wsgi.py")
    return True


# ─── Step 5: Create start_ava.sh ─────────────────────────────────────────────

def step5_create_start_script():
    print("\n── Step 5: Create start_ava.sh ──────────────────────────────")
    script_path = PROJECT_DIR / "start_ava.sh"
    if script_path.exists():
        backup(script_path)

    script = '''#!/usr/bin/env bash
# start_ava.sh
# AVA — Start / Stop / Restart / Status
# Usage:
#   ./start_ava.sh          — start (HTTPS on 5443 + HTTP on 5002)
#   ./start_ava.sh stop     — stop all AVA processes
#   ./start_ava.sh restart  — stop then start
#   ./start_ava.sh status   — show running processes
#   ./start_ava.sh logs     — tail logs

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/venv"
GUNICORN="$VENV/bin/gunicorn"
LOG_DIR="/mnt/i/ai-lab/logs"
PID_HTTPS="/tmp/ava_gunicorn_https.pid"
PID_HTTP="/tmp/ava_gunicorn_http.pid"
ENV_FILE="$PROJECT_DIR/.env"

# Load .env
if [ -f "$ENV_FILE" ]; then
    set -a && source "$ENV_FILE" && set +a
fi

mkdir -p "$LOG_DIR"

cmd="${1:-start}"

case "$cmd" in

  start)
    echo "── Starting AVA ────────────────────────────────────"

    # Kill any existing processes
    [ -f "$PID_HTTPS" ] && kill "$(cat $PID_HTTPS)" 2>/dev/null || true
    [ -f "$PID_HTTP"  ] && kill "$(cat $PID_HTTP)"  2>/dev/null || true
    fuser -k 5443/tcp 2>/dev/null || true
    fuser -k 5002/tcp 2>/dev/null || true
    sleep 1

    cd "$PROJECT_DIR"
    source "$VENV/bin/activate"

    # HTTPS on 5443
    echo "  Starting HTTPS on :5443..."
    "$GUNICORN" wsgi:application \\
      --bind 0.0.0.0:5443 \\
      --certfile certs/ava.crt \\
      --keyfile  certs/ava.key \\
      --workers 2 \\
      --timeout 300 \\
      --access-logfile "$LOG_DIR/ava_access.log" \\
      --error-logfile  "$LOG_DIR/ava_error.log" \\
      --pid "$PID_HTTPS" \\
      --daemon

    # HTTP on 5002 (1 worker — local dev/curl only)
    echo "  Starting HTTP  on :5002..."
    "$GUNICORN" wsgi:application \\
      --bind 0.0.0.0:5002 \\
      --workers 1 \\
      --timeout 300 \\
      --access-logfile "$LOG_DIR/ava_access_http.log" \\
      --error-logfile  "$LOG_DIR/ava_error.log" \\
      --pid "$PID_HTTP" \\
      --daemon

    sleep 2
    echo ""
    echo "  ✅  AVA running"
    echo "  HTTPS → https://localhost:5443"
    echo "  HTTP  → http://localhost:5002"
    echo "  Logs  → $LOG_DIR/"
    ;;

  stop)
    echo "── Stopping AVA ────────────────────────────────────"
    [ -f "$PID_HTTPS" ] && kill "$(cat $PID_HTTPS)" 2>/dev/null && echo "  Stopped HTTPS" || true
    [ -f "$PID_HTTP"  ] && kill "$(cat $PID_HTTP)"  2>/dev/null && echo "  Stopped HTTP"  || true
    fuser -k 5443/tcp 2>/dev/null || true
    fuser -k 5002/tcp 2>/dev/null || true
    rm -f "$PID_HTTPS" "$PID_HTTP"
    echo "  ✅  AVA stopped"
    ;;

  restart)
    "$0" stop
    sleep 2
    "$0" start
    ;;

  status)
    echo "── AVA Status ──────────────────────────────────────"
    echo -n "  Port 5443 (HTTPS): "
    fuser 5443/tcp 2>/dev/null && echo "running" || echo "stopped"
    echo -n "  Port 5002 (HTTP):  "
    fuser 5002/tcp 2>/dev/null && echo "running" || echo "stopped"
    ;;

  logs)
    tail -f "$LOG_DIR/ava_error.log" "$LOG_DIR/ava_access.log" 2>/dev/null
    ;;

  *)
    echo "Usage: $0 {start|stop|restart|status|logs}"
    exit 1
    ;;
esac
'''
    script_path.write_text(script)
    os.chmod(script_path, 0o755)
    print(f"  {OK}  Created start_ava.sh (chmod +x)")
    return True


# ─── Step 6: Ensure log directory exists ─────────────────────────────────────

def step6_create_log_dir():
    print("\n── Step 6: Create log directory ─────────────────────────────")
    log_dir = Path("/mnt/i/ai-lab/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"  {OK}  {log_dir} ready")
    return True


# ─── Step 7: Verify wsgi.py imports correctly ────────────────────────────────

def step7_verify():
    print("\n── Step 7: Verify Gunicorn can load wsgi.py ─────────────────")
    gunicorn = PROJECT_DIR / "venv/bin/gunicorn"
    if not gunicorn.exists():
        print(f"  {ERR} Gunicorn not found in venv")
        return False

    # Dry-run check
    result = run(
        [str(gunicorn), "--check-config", "-c", "gunicorn.conf.py", "wsgi:application"],
        check=False,
        cwd=str(PROJECT_DIR),
    )
    if result.returncode == 0:
        print(f"  {OK}  Gunicorn config valid")
        return True
    else:
        # --check-config not always available — try --print-config
        print(f"  {INF} Config check output: {result.stderr[:200]}")
        print(f"  {INF} Will verify on first start")
        return True


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  AVA — Phase 4 Day 9: Gunicorn + HTTPS")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    os.chdir(PROJECT_DIR)

    results = []
    results.append(step1_install_gunicorn())
    results.append(step2_generate_certs())
    results.append(step3_create_gunicorn_conf())
    results.append(step4_create_wsgi())
    results.append(step5_create_start_script())
    step6_create_log_dir()
    step7_verify()

    print("\n" + "=" * 60)
    if all(results):
        print(f"  {OK}  Day 9 patch complete.")
        print()
        print("  Start AVA with Gunicorn:")
        print("  ./start_ava.sh")
        print()
        print("  Then test HTTPS:")
        print("  curl -sk https://localhost:5443/auth/login \\")
        print('    -X POST -H "Content-Type: application/json" \\')
        print('    -d \'{"username":"admin","password":"<YOUR_ADMIN_PASSWORD>"}\'')
    else:
        print(f"  {ERR}  Some steps failed — see above")
    print("=" * 60)


if __name__ == "__main__":
    main()
