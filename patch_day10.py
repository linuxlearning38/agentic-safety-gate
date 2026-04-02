#!/usr/bin/env python3
"""
AVA Phase 4 Day 10 — Pre-Docker Patch
Converts hardcoded /mnt/i/ai-lab/... paths and log paths to env-driven.
Run this BEFORE building the Docker image.

Patches:
  web_agent_v2.1_guardrail.py → CHROMA_PATH, HISTORY_FILE, MEMORY_PATH
  gunicorn.conf.py            → accesslog, errorlog
"""
import shutil, datetime, sys
from pathlib import Path

BASE = Path(__file__).parent

APP      = BASE / "web_agent_v2.1_guardrail.py"
GUNICORN = BASE / "gunicorn.conf.py"

def backup(path: Path) -> Path:
    ts  = datetime.datetime.now().strftime("%H%M%S")
    dst = path.with_suffix(f".py.bak_day10_{ts}")
    shutil.copy2(path, dst)
    return dst

# ── Patch 1: web_agent_v2.1_guardrail.py ─────────────────────────────────────

APP_PATCHES = [
    (
        'CHROMA_PATH = "/mnt/i/ai-lab/chromadb"',
        'CHROMA_PATH = os.getenv("CHROMA_PATH", "/mnt/i/ai-lab/chromadb")',
    ),
    (
        'HISTORY_FILE = "/mnt/i/ai-lab/projects/devops-agent/query_history.json"',
        'HISTORY_FILE = os.getenv("HISTORY_FILE", "/mnt/i/ai-lab/projects/devops-agent/query_history.json")',
    ),
    (
        'MEMORY_PATH = "/mnt/i/ai-lab/ava_memory.json"',
        'MEMORY_PATH = os.getenv("MEMORY_PATH", "/mnt/i/ai-lab/ava_memory.json")',
    ),
]

# ── Patch 2: gunicorn.conf.py ─────────────────────────────────────────────────
# Adds `import os` at top if missing, then makes log paths env-driven.

GUNICORN_PATCHES = [
    (
        'accesslog    = "/mnt/i/ai-lab/logs/ava_access.log"',
        'accesslog    = os.getenv("GUNICORN_ACCESS_LOG", "/mnt/i/ai-lab/logs/ava_access.log")',
    ),
    (
        'errorlog     = "/mnt/i/ai-lab/logs/ava_error.log"',
        'errorlog     = os.getenv("GUNICORN_ERROR_LOG",  "/mnt/i/ai-lab/logs/ava_error.log")',
    ),
]

def apply_patches(path: Path, patches: list, label: str):
    print(f"\n── {label} ──")
    bak = backup(path)
    print(f"  Backup → {bak.name}")

    content = path.read_text()
    applied = 0

    for old, new in patches:
        if old in content:
            content = content.replace(old, new, 1)
            print(f"  ✅ Patched: {old[:70]}")
            applied += 1
        elif new in content:
            print(f"  ⏭  Already patched: {old[:70]}")
        else:
            print(f"  ❌ NOT FOUND — check manually: {old[:70]}")

    path.write_text(content)
    print(f"  Applied {applied}/{len(patches)} patches.")
    return applied

def ensure_os_import(path: Path):
    """Prepend `import os` to gunicorn.conf.py if not already present."""
    content = path.read_text()
    if "import os" not in content:
        content = "import os\n" + content
        path.write_text(content)
        print("  ✅ Added `import os` at top of gunicorn.conf.py")
    else:
        print("  ⏭  `import os` already present in gunicorn.conf.py")

# ── Run ───────────────────────────────────────────────────────────────────────

if not APP.exists():
    print(f"ERROR: {APP} not found. Run this from the devops-agent directory.")
    sys.exit(1)
if not GUNICORN.exists():
    print(f"ERROR: {GUNICORN} not found. Run this from the devops-agent directory.")
    sys.exit(1)

apply_patches(APP, APP_PATCHES, "web_agent_v2.1_guardrail.py")

ensure_os_import(GUNICORN)
apply_patches(GUNICORN, GUNICORN_PATCHES, "gunicorn.conf.py")

print("""
── Verify ───────────────────────────────────────────────────────────────
grep -n 'os.getenv' web_agent_v2.1_guardrail.py | grep -E 'CHROMA|HISTORY|MEMORY'
grep -n 'os.getenv' gunicorn.conf.py

── Next ─────────────────────────────────────────────────────────────────
# Create ava_memory.json if missing (bind mount fails on missing files)
[ -f /mnt/i/ai-lab/ava_memory.json ] || echo '{}' > /mnt/i/ai-lab/ava_memory.json

# Generate requirements.txt from live env
pip freeze > requirements.txt

# Build and start
docker compose build ava
docker compose up -d ava
""")
