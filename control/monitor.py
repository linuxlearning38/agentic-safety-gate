"""
control/monitor.py
Phase 5C — Background health monitor for AVA.

Runs every 60 seconds in a daemon thread:
  1. docker ps  — detect exited/unhealthy containers
  2. df /data   — detect disk pressure > 85 %
  3. free -m    — detect memory pressure > 90 %

Findings are passed to SelfHealer for automatic remediation.
All subprocess calls use shell=False.
"""

import logging
import subprocess
import threading
import time

logger = logging.getLogger("ava.monitor")

_INTERVAL_SEC = 60
_started      = False
_lock         = threading.Lock()


# ─── Check helpers ────────────────────────────────────────────────────────────

def _check_containers() -> list[dict]:
    """Return list of {name, status} for any non-running containers."""
    issues = []
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            name, status = parts
            if not status.lower().startswith("up"):
                issues.append({"name": name, "status": status})
    except Exception as e:
        logger.warning(f"[Monitor] docker ps failed: {e}")
    return issues


def _check_disk() -> dict | None:
    """Return disk issue dict if /data usage > 85 %, else None."""
    try:
        result = subprocess.run(
            ["df", "/data"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                pct = int(parts[4].rstrip("%"))
                if pct > 85:
                    return {"path": "/data", "usage_pct": pct}
    except Exception as e:
        logger.warning(f"[Monitor] df failed: {e}")
    return None


def _check_memory() -> dict | None:
    """Return memory issue dict if used > 90 % of total, else None."""
    try:
        result = subprocess.run(
            ["free", "-m"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
        for line in result.stdout.splitlines():
            if line.lower().startswith("mem"):
                parts = line.split()
                if len(parts) >= 3:
                    total = int(parts[1])
                    used  = int(parts[2])
                    if total > 0 and (used / total) > 0.90:
                        return {"used_mb": used, "total_mb": total,
                                "pct": round(used / total * 100, 1)}
    except Exception as e:
        logger.warning(f"[Monitor] free failed: {e}")
    return None


# ─── Main loop ────────────────────────────────────────────────────────────────

def _run_monitor() -> None:
    # Import here to avoid circular import at module load time
    from control.self_healer import healer

    logger.info("[Monitor] Background health monitor started (interval=60s)")

    while True:
        try:
            # 1 — Container health
            down = _check_containers()
            for container in down:
                msg = f"container {container['name']} is not running: {container['status']}"
                logger.warning(f"[Monitor] {msg}")
                issue = healer.detect_issue(source="monitor:docker", message=msg)
                healer.heal(issue, dry_run=False)

            # 2 — Disk pressure
            disk = _check_disk()
            if disk:
                msg = f"disk full: /data at {disk['usage_pct']}% usage"
                logger.warning(f"[Monitor] {msg}")
                issue = healer.detect_issue(source="monitor:disk", message=msg)
                healer.heal(issue, dry_run=False)

            # 3 — Memory pressure
            mem = _check_memory()
            if mem:
                msg = f"high memory usage: {mem['used_mb']}MB / {mem['total_mb']}MB ({mem['pct']}%)"
                logger.warning(f"[Monitor] {msg}")
                issue = healer.detect_issue(source="monitor:memory", message=msg)
                healer.heal(issue, dry_run=False)

            if not (down or disk or mem):
                logger.debug("[Monitor] All checks passed")

        except Exception as e:
            logger.error(f"[Monitor] Unexpected error in monitor loop: {e}")

        time.sleep(_INTERVAL_SEC)


def start_monitor() -> None:
    """Start the background monitor thread (idempotent — safe to call multiple times)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    t = threading.Thread(target=_run_monitor, name="ava-monitor", daemon=True)
    t.start()
    logger.info("[Monitor] Monitor thread launched")
