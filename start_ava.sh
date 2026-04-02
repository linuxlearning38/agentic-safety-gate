#!/usr/bin/env bash
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
    "$GUNICORN" wsgi:application \
      --bind 0.0.0.0:5443 \
      --certfile certs/ava.crt \
      --keyfile  certs/ava.key \
      --workers 2 \
      --timeout 300 \
      --access-logfile "$LOG_DIR/ava_access.log" \
      --error-logfile  "$LOG_DIR/ava_error.log" \
      --pid "$PID_HTTPS" \
      --daemon

    # HTTP on 5002 (1 worker — local dev/curl only)
    echo "  Starting HTTP  on :5002..."
    "$GUNICORN" wsgi:application --config "" \
      --bind 0.0.0.0:5002 \
      --workers 1 \
      --timeout 300 \
      --access-logfile "$LOG_DIR/ava_access_http.log" \
      --error-logfile  "$LOG_DIR/ava_error.log" \
      --pid "$PID_HTTP" \
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
