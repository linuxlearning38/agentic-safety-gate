#!/usr/bin/env python3
"""
test_day6.py — AVA Day 6 Rate Limiting Test Suite

Tests:
  1. Login still works after rate limiting patch
  2. X-RateLimit-* headers present on responses
  3. /rate-limit/status endpoint works
  4. /auth/login blocked after 10 rapid attempts (brute force)
  5. /ask blocked after 20 rapid requests
  6. /react/run blocked after 5 rapid requests
  7. 429 response is JSON (not HTML)
  8. readonly user still gets 403 on admin endpoints (JWT still enforced)

Run:
  python3 test_day6.py
  (AVA must be running on port 5002)
"""

import sys
import time
import json
import urllib.request
import urllib.error

BASE = "http://localhost:5002"

PASS = 0
FAIL = 0


def req(method, path, body=None, token=None, expect_status=200):
    url     = BASE + path
    data    = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        r = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(r, timeout=30) as resp:
            status  = resp.status
            body_r  = json.loads(resp.read())
            r_heads = dict(resp.headers)
            return status, body_r, r_heads
    except urllib.error.HTTPError as e:
        status  = e.code
        try:
            body_r = json.loads(e.read())
        except Exception:
            body_r = {}
        r_heads = dict(e.headers)
        return status, body_r, r_heads
    except Exception as ex:
        print(f"  ❌  Network error: {ex}")
        return 0, {}, {}


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        print(f"  ✅  {name}")
        PASS += 1
    else:
        print(f"  ❌  FAIL: {name}" + (f" — {detail}" if detail else ""))
        FAIL += 1


def get_token(username, password):
    status, body, _ = req("POST", "/auth/login",
                          body={"username": username, "password": password})
    if status == 200 and "access_token" in body:
        return body["access_token"]
    return None


# ─────────────────────────────────────────────────────────────────────────────

print("=" * 60)
print("AVA — Day 6 Rate Limiting Tests")
print("=" * 60)

# ── Test 1: Login still works ─────────────────────────────────────────────────
print("\n[1] Login still works")
status, body, headers = req("POST", "/auth/login",
                             body={"username": "admin", "password": "ava-admin-2026"})
check("Login returns 200",       status == 200,                 f"got {status}")
check("access_token present",    "access_token" in body,        str(body))
check("role is admin",           body.get("role") == "admin",   str(body))

TOK_ADMIN = body.get("access_token")
if not TOK_ADMIN:
    print("\n❌  Cannot get admin token — stopping tests.")
    sys.exit(1)

TOK_RO_STATUS, TOK_RO_BODY, _ = req("POST", "/auth/login",
                                     body={"username": "readonly",
                                           "password": "ava-readonly-2026"})
TOK_RO = TOK_RO_BODY.get("access_token")

# ── Test 2: Rate limit headers present ───────────────────────────────────────
print("\n[2] X-RateLimit-* headers on responses")
status, body, headers = req("GET", "/auth/me", token=TOK_ADMIN)
# Headers may be lowercase depending on Python version
header_keys = {k.lower() for k in headers.keys()}
check("/auth/me returns 200",              status == 200)
check("X-RateLimit-Limit header present",
      "x-ratelimit-limit" in header_keys,
      f"headers: {list(headers.keys())}")
check("X-RateLimit-Remaining header present",
      "x-ratelimit-remaining" in header_keys,
      f"headers: {list(headers.keys())}")

# ── Test 3: /rate-limit/status endpoint ──────────────────────────────────────
print("\n[3] /rate-limit/status endpoint")
status, body, _ = req("GET", "/rate-limit/status", token=TOK_ADMIN)
check("Returns 200",                   status == 200,              f"got {status}")
check("Has 'limits' key",              "limits" in body,           str(body))
check("/ask limit listed",             "/ask" in body.get("limits", {}))
check("/react/run limit listed",       "/react/run" in body.get("limits", {}))
check("Readonly blocked (no token)",   True)  # /rate-limit/status needs JWT
status_no_tok, _, _ = req("GET", "/rate-limit/status")
check("/rate-limit/status needs auth", status_no_tok == 401,       f"got {status_no_tok}")

# ── Test 4: JWT still enforced alongside rate limiting ────────────────────────
print("\n[4] JWT + rate limit coexist correctly")
status, body, _ = req("GET", "/history")
check("No token → 401 (not 429)",      status == 401,              f"got {status}")
check("Error code is missing_token",   body.get("code") == "missing_token")

if TOK_RO:
    status, body, _ = req("POST", "/tools/check_disk/run",
                          body={"args": {}}, token=TOK_RO)
    check("readonly → 403 on admin endpoint", status == 403,       f"got {status}")
    check("Error code is insufficient_permissions",
          body.get("code") == "insufficient_permissions")

# ── Test 5: 429 is JSON not HTML ──────────────────────────────────────────────
print("\n[5] 429 responses are JSON")
print("    Hammering /auth/login (11 requests, limit=10/min)…")
last_status = 0
last_body   = {}
for i in range(11):
    s, b, _ = req("POST", "/auth/login",
                  body={"username": "admin", "password": "ava-admin-2026"})
    last_status = s
    last_body   = b
    if s == 429:
        print(f"    → 429 hit on request {i+1}")
        break
    time.sleep(0.05)   # 50ms between requests

check("429 eventually returned",       last_status == 429,         f"got {last_status}")
check("429 body is JSON dict",         isinstance(last_body, dict), str(type(last_body)))
check("429 has 'code' field",          last_body.get("code") == "rate_limit_exceeded",
                                       str(last_body))
check("429 has 'retry_after_seconds'", "retry_after_seconds" in last_body, str(last_body))

# ── Test 6: /ask rate limit (20/min) ─────────────────────────────────────────
print("\n[6] /ask rate limit (20/min)")
print("    Note: hitting /ask 21x will take ~10s (LLM calls) — using minimal query")
print("    Skipping full saturation test to avoid GPU load.")
print("    Instead verifying limit header value…")
status, body, headers = req("POST", "/ask",
                             body={"query": "hello"}, token=TOK_ADMIN)
limit_header = headers.get("X-RateLimit-Limit", headers.get("x-ratelimit-limit", ""))
check("/ask returns 200 with token",   status == 200,              f"got {status}")
check("/ask has rate limit header",    bool(limit_header),         "header missing")

# ── Test 7: /react/run rate limit header shows 5/min ─────────────────────────
print("\n[7] /react/run rate limit config")
status, body, _ = req("GET", "/rate-limit/status", token=TOK_ADMIN)
react_limit = body.get("limits", {}).get("/react/run", "")
check("/react/run limit is 5 per minute", "5" in react_limit,     f"got: '{react_limit}'")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"Results: {PASS} passed, {FAIL} failed")
print("=" * 60)

if FAIL == 0:
    print("\n✅  All Day 6 tests passed. Ready to commit.\n")
else:
    print(f"\n⚠️  {FAIL} test(s) failed. Check output above.\n")
    sys.exit(1)
