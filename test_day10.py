#!/usr/bin/env python3
"""
AVA Phase 4 Day 10 — Docker Test Suite
Tests containerized AVA: container state, HTTPS, env vars, ChromaDB, Ollama.
Run AFTER `docker compose up -d ava` and container reaches healthy state.

Usage:
    python3 test_day10.py
"""
import subprocess, sys, time
import urllib.request, urllib.error, ssl, json

HTTPS_BASE   = "https://localhost:5443"
ADMIN_CREDS  = {"username": "admin", "password": "ava-admin-2026"}
CONTAINER    = "ava-agent"
IMAGE        = "ava-agent:latest"

# ── Helpers ───────────────────────────────────────────────────────────────────

def https(method: str, path: str, body=None, token: str = None, timeout=15):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        f"{HTTPS_BASE}{path}", data=data, headers=headers, method=method
    )
    resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
    return resp.status, json.loads(resp.read())

def docker_exec(*cmd):
    r = subprocess.run(
        ["docker", "exec", CONTAINER, *cmd],
        capture_output=True, text=True, timeout=15
    )
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def docker_run(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def get_token() -> str:
    status, data = https("POST", "/auth/login", body=ADMIN_CREDS)
    assert status == 200 and "access_token" in data
    return data["access_token"]

# ── Test runner ───────────────────────────────────────────────────────────────

results = []

def test(name: str, fn):
    try:
        fn()
        results.append((name, "PASS", ""))
        print(f"  ✅  {name}")
    except Exception as e:
        results.append((name, "FAIL", str(e)))
        print(f"  ❌  {name}: {e}")

# ── Tests ─────────────────────────────────────────────────────────────────────

def t_container_running():
    out, _, _ = docker_run("docker", "ps", "--filter", f"name={CONTAINER}", "--format", "{{.Names}}")
    assert CONTAINER in out, f"Container not found in `docker ps`. Got: {out!r}"

def t_container_healthy():
    out, _, _ = docker_run("docker", "inspect", "--format", "{{.State.Health.Status}}", CONTAINER)
    assert out in ("healthy", "starting"), f"Health status: {out!r}"

def t_nonroot_user():
    out, err, rc = docker_exec("whoami")
    assert rc == 0, f"whoami failed: {err}"
    assert out == "ava", f"Expected user 'ava', got: {out!r}"

def t_env_chroma():
    out, _, _ = docker_exec("env")
    assert "CHROMA_PATH=/data/chromadb" in out, \
        "CHROMA_PATH not set to /data/chromadb in container"

def t_env_history():
    out, _, _ = docker_exec("env")
    assert "HISTORY_FILE=/data/history/query_history.json" in out, \
        "HISTORY_FILE not set correctly in container"

def t_env_memory():
    out, _, _ = docker_exec("env")
    assert "MEMORY_PATH=/data/ava_memory.json" in out, \
        "MEMORY_PATH not set correctly in container"

def t_env_ollama():
    out, _, _ = docker_exec("env")
    assert "OLLAMA_HOST=http://host.docker.internal:11434" in out, \
        "OLLAMA_HOST not set correctly in container"

def t_https_reachable():
    status, _ = https("GET", "/health")
    assert status == 200, f"Expected 200, got {status}"

def t_login():
    status, data = https("POST", "/auth/login", body=ADMIN_CREDS)
    assert status == 200, f"Login returned {status}"
    assert "access_token" in data, f"No access_token in response: {data}"

def t_unauth_rejected():
    try:
        https("GET", "/auth/me")
        raise AssertionError("Expected 401 but got 200")
    except urllib.error.HTTPError as e:
        assert e.code == 401, f"Expected 401, got {e.code}"

def t_tools_endpoint():
    token = get_token()
    status, data = https("GET", "/tools", token=token)
    assert status == 200, f"Expected 200, got {status}"

def t_chromadb_accessible():
    """
    Verifies ChromaDB volume is correctly mounted and readable.
    /stats endpoint returns collection chunk counts — fails if ChromaDB is unreachable.
    """
    token = get_token()
    status, data = https("GET", "/stats", token=token, timeout=20)
    assert status == 200, f"/stats returned {status}"
    # ChromaDB responds → data will contain collection info
    data_str = json.dumps(data).lower()
    assert any(k in data_str for k in ("chunk", "collection", "policy", "blog")), \
        f"ChromaDB data not visible in /stats response: {data}"

def t_ollama_reachable():
    """Verifies host Ollama is reachable via host.docker.internal from inside container."""
    out, err, rc = docker_exec(
        "python3", "-c",
        "import urllib.request; urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=5); print('reachable')"
    )
    assert "reachable" in out, f"Ollama unreachable from container. stderr: {err}"

def t_logs_written():
    """Verifies Gunicorn access log is being written to the mounted /data/logs/ volume."""
    out, err, rc = docker_exec("ls", "-la", "/data/logs/")
    assert rc == 0, f"Could not list /data/logs/: {err}"
    assert ".log" in out, f"No .log files in /data/logs/: {out}"

def t_image_size():
    out, _, _ = docker_run("docker", "image", "inspect", IMAGE, "--format", "{{.Size}}")
    size_bytes = int(out.strip())
    size_mb    = size_bytes // (1024 ** 2)
    assert size_bytes < 2 * 1024 ** 3, f"Image too large: {size_mb}MB (limit: 2048MB)"
    print(f"       Image size: {size_mb}MB", end="")

# ── Run suite ─────────────────────────────────────────────────────────────────

print("\nAVA Phase 4 Day 10 — Docker Test Suite")
print("=" * 52)

print("\n── Container State ───────────────────────────────")
test("Container ava-agent running",   t_container_running)
test("Container health status",       t_container_healthy)
test("Running as non-root user (ava)", t_nonroot_user)

print("\n── Environment Variables ─────────────────────────")
test("CHROMA_PATH → /data/chromadb",                t_env_chroma)
test("HISTORY_FILE → /data/history/...",            t_env_history)
test("MEMORY_PATH → /data/ava_memory.json",         t_env_memory)
test("OLLAMA_HOST → host.docker.internal:11434",    t_env_ollama)

print("\n── HTTPS Endpoints ───────────────────────────────")
test("HTTPS :5443 reachable",          t_https_reachable)
test("POST /auth/login → 200 + token", t_login)
test("Unauthenticated request → 401",  t_unauth_rejected)
test("GET /tools → 200",               t_tools_endpoint)

print("\n── Integration ───────────────────────────────────")
test("ChromaDB volume accessible",          t_chromadb_accessible)
test("Ollama reachable via host-gateway",   t_ollama_reachable)
test("Gunicorn logs written to volume",     t_logs_written)

print("\n── Image ─────────────────────────────────────────")
test("Image size < 2GB", t_image_size)

# ── Summary ───────────────────────────────────────────────────────────────────

print()
passed = sum(1 for _, s, _ in results if s == "PASS")
failed = sum(1 for _, s, _ in results if s == "FAIL")
total  = len(results)

print("=" * 52)
print(f"Results: {passed}/{total} passed | {failed} failed")

if failed:
    print("\nFailed tests:")
    for name, status, err in results:
        if status == "FAIL":
            print(f"  ✗ {name}: {err}")
    sys.exit(1)
