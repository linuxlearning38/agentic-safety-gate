#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start_ava.sh — Day 10 additions
#
# These are the NEW cases to add inside the existing case "$1" in block,
# BEFORE the closing `esac` line.
#
# Paste the block below into start_ava.sh, just before `esac`:
# ─────────────────────────────────────────────────────────────────────────────

# ── HOW TO APPLY ─────────────────────────────────────────────────────────────
# In your existing start_ava.sh, find the line that reads:
#     esac
# and insert the block below immediately above it.
# ─────────────────────────────────────────────────────────────────────────────

# ── PASTE FROM HERE ───────────────────────────────────────────────────────────

  docker-start)
    echo "[AVA] Building and starting Docker container..."

    # Ensure bind-mount files exist — Docker fails if target file is missing
    if [ ! -f /mnt/i/ai-lab/ava_memory.json ]; then
      echo "[AVA] Creating missing ava_memory.json..."
      echo '{}' > /mnt/i/ai-lab/ava_memory.json
    fi

    if [ ! -f /mnt/i/ai-lab/projects/devops-agent/query_history.json ]; then
      echo "[AVA] Creating missing query_history.json..."
      echo '[]' > /mnt/i/ai-lab/projects/devops-agent/query_history.json
    fi

    docker compose build ava && \
    docker compose up -d ava

    echo ""
    echo "[AVA] Container started. Waiting for health check..."
    for i in $(seq 1 12); do
      STATUS=$(docker inspect --format='{{.State.Health.Status}}' ava-agent 2>/dev/null)
      if [ "$STATUS" = "healthy" ]; then
        echo "[AVA] ✅ Container healthy → https://localhost:5443"
        break
      fi
      echo "[AVA] Health: $STATUS (attempt $i/12)..."
      sleep 5
    done
    ;;

  docker-stop)
    echo "[AVA] Stopping Docker container..."
    docker compose stop ava
    docker compose rm -f ava
    echo "[AVA] Container stopped."
    ;;

  docker-restart)
    "$0" docker-stop
    sleep 2
    "$0" docker-start
    ;;

  docker-logs)
    docker logs -f ava-agent
    ;;

  docker-status)
    echo "── Container ───────────────────────────────"
    docker ps --filter "name=ava-agent" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    echo ""
    echo "── Health ──────────────────────────────────"
    docker inspect --format='Health: {{.State.Health.Status}}' ava-agent 2>/dev/null || echo "Container not running."
    echo ""
    echo "── Port check ──────────────────────────────"
    curl -sk --max-time 3 https://localhost:5443/health && echo " → :5443 OK" || echo " → :5443 unreachable"
    ;;

  docker-shell)
    docker exec -it ava-agent /bin/bash 2>/dev/null || docker exec -it ava-agent /bin/sh
    ;;

# ── PASTE TO HERE ─────────────────────────────────────────────────────────────
