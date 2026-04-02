#!/usr/bin/env python3
"""
patch_day6.py — AVA Phase 4, Day 6: Rate Limiting

What this patch does:
  1. Installs Flask-Limiter with in-memory storage (no Redis needed)
  2. Key function: JWT username when authenticated, IP address for public endpoints
  3. Per-endpoint limits:
       /auth/login          → 10/minute  per IP  (brute force protection)
       /ask                 → 20/minute  per user (LLM calls expensive)
       /tools/<n>/run       → 10/minute  per user
       /execute_approved    → 10/minute  per user
       /react/run           →  5/minute  per user (ReAct is very expensive)
       everything else      → 30/minute  per user (global default)
  4. X-RateLimit-* headers on every response
  5. 429 handler returns JSON (not Flask's default HTML)
  6. New endpoint: GET /rate-limit/status — shows current limits per user
  7. Rate limit events logged at WARNING level for audit

Run:
  pip install flask-limiter
  python3 patch_day6.py

After running:
  fuser -k 5002/tcp && sleep 1
  set -a && source .env && set +a
  python3 web_agent_v2.1_guardrail.py
"""

import os
import sys
import shutil
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_FILE   = os.path.join(PROJECT_DIR, "web_agent_v2.1_guardrail.py")

# ─────────────────────────────────────────────────────────────────────────────
# Patch strings
# ─────────────────────────────────────────────────────────────────────────────

# 1. Add limiter imports after JWT imports
OLD_JWT_IMPORT = "from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt\nfrom control.auth import init_jwt, verify_credentials, require_admin, make_token"

NEW_JWT_IMPORT = """from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt, decode_token
from control.auth import init_jwt, verify_credentials, require_admin, make_token
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_limiter.errors import RateLimitExceeded"""

# 2. Init limiter after JWT init
OLD_JWT_INIT = "jwt_manager = init_jwt(app)"

NEW_JWT_INIT = """jwt_manager = init_jwt(app)

# ── Day 6: Rate Limiting ──────────────────────────────────────────────────────
def _rate_limit_key() -> str:
    \"\"\"
    Rate limit key function.
    Authenticated requests: keyed by JWT username → per-user limits.
    Unauthenticated requests: keyed by IP → prevents login brute force.
    \"\"\"
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            decoded = decode_token(auth[7:], allow_expired=False)
            return f"user:{decoded.get('sub', 'unknown')}"
        except Exception:
            pass
    return f"ip:{get_remote_address()}"

limiter = Limiter(
    app=app,
    key_func=_rate_limit_key,
    default_limits=["30 per minute"],
    storage_uri="memory://",
    headers_enabled=True,       # X-RateLimit-Limit/Remaining/Reset on every response
    swallow_errors=True,        # don't crash AVA if limiter storage fails
    on_breach=_on_rate_limit_breach,
)

def _on_rate_limit_breach(limit):
    key = _rate_limit_key()
    logger.warning(
        f"[RateLimit] BREACH: key='{key}' "
        f"limit='{limit.limit}' "
        f"endpoint='{request.endpoint}' "
        f"path='{request.path}'"
    )"""

# Note: _on_rate_limit_breach must be defined BEFORE limiter init
# We split the init to handle this correctly
FIXED_JWT_INIT = """jwt_manager = init_jwt(app)

# ── Day 6: Rate Limiting ──────────────────────────────────────────────────────
def _rate_limit_key() -> str:
    \"\"\"
    Rate limit key function.
    Authenticated requests: keyed by JWT username → per-user limits.
    Unauthenticated requests: keyed by IP address → brute force protection.
    \"\"\"
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            decoded = decode_token(auth[7:], allow_expired=False)
            return f"user:{decoded.get('sub', 'unknown')}"
        except Exception:
            pass
    return f"ip:{get_remote_address()}"

def _on_rate_limit_breach(limit):
    key = _rate_limit_key()
    logger.warning(
        f"[RateLimit] BREACH: key='{key}' "
        f"limit='{limit.limit}' "
        f"endpoint='{request.endpoint}' "
        f"path='{request.path}'"
    )

limiter = Limiter(
    app=app,
    key_func=_rate_limit_key,
    default_limits=["30 per minute"],
    storage_uri="memory://",
    headers_enabled=True,
    swallow_errors=True,
    on_breach=_on_rate_limit_breach,
)
logger.info("[RateLimit] Flask-Limiter initialised — default: 30 req/min per user")"""

# 3. 429 error handler — inject after existing error handlers
OLD_ERROR_HANDLERS = """@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error'}), 500"""

NEW_ERROR_HANDLERS = """@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(RateLimitExceeded)
def rate_limit_exceeded_handler(e):
    \"\"\"Return JSON on 429 — not Flask's default HTML error page.\"\"\"
    retry_after = int(e.retry_after) if hasattr(e, 'retry_after') else 60
    return jsonify({
        "error":               "Rate limit exceeded. Slow down.",
        "code":                "rate_limit_exceeded",
        "limit":               str(e.limit.limit),
        "retry_after_seconds": retry_after,
    }), 429"""

# 4. /auth/login — 10/min per IP (brute force)
OLD_LOGIN_ROUTE = "@app.route('/auth/login', methods=['POST'])\ndef auth_login():"

NEW_LOGIN_ROUTE = """@app.route('/auth/login', methods=['POST'])
@limiter.limit("10 per minute", key_func=get_remote_address)
def auth_login():"""

# 5. /ask — 20/min (LLM calls expensive)
OLD_ASK_ROUTE = "@app.route('/ask', methods=['POST'])\n@jwt_required()\ndef ask():"

NEW_ASK_ROUTE = """@app.route('/ask', methods=['POST'])
@jwt_required()
@limiter.limit("20 per minute")
def ask():"""

# 6. /execute_approved — 10/min admin only
OLD_EXEC_ROUTE = "@app.route('/execute_approved', methods=['POST'])\n@require_admin\ndef execute_approved_route():"

NEW_EXEC_ROUTE = """@app.route('/execute_approved', methods=['POST'])
@require_admin
@limiter.limit("10 per minute")
def execute_approved_route():"""

# 7. /tools/<n>/run — 10/min admin only
OLD_TOOL_RUN_ROUTE = "@app.route('/tools/<tool_name>/run', methods=['POST'])\n@require_admin\ndef run_tool_route(tool_name):"

NEW_TOOL_RUN_ROUTE = """@app.route('/tools/<tool_name>/run', methods=['POST'])
@require_admin
@limiter.limit("10 per minute")
def run_tool_route(tool_name):"""

# 8. /react/run — 5/min (most expensive endpoint)
OLD_REACT_ROUTE = "@app.route('/react/run', methods=['POST'])\n@require_admin\ndef react_run_route():"

NEW_REACT_ROUTE = """@app.route('/react/run', methods=['POST'])
@require_admin
@limiter.limit("5 per minute")
def react_run_route():"""

# 9. Rate limit status endpoint — inject before error handlers
OLD_BEFORE_ERRORS = "@app.errorhandler(404)\ndef not_found(e):"

NEW_BEFORE_ERRORS = """@app.route('/rate-limit/status', methods=['GET'])
@jwt_required()
def rate_limit_status():
    \"\"\"
    GET /rate-limit/status
    Shows current rate limit config for the authenticated user.
    Useful for debugging and monitoring.
    \"\"\"
    identity = get_jwt_identity()
    claims   = get_jwt()
    role     = claims.get("role", "unknown")

    limits = {
        "user":    identity,
        "role":    role,
        "limits":  {
            "default":           "30 per minute",
            "/ask":              "20 per minute",
            "/tools/<n>/run":    "10 per minute",
            "/execute_approved": "10 per minute",
            "/react/run":        "5 per minute",
            "/auth/login":       "10 per minute (per IP, unauthenticated)",
        },
        "storage": "in-memory (resets on restart)",
        "headers": "X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset",
        "note":    "Rate limits are per-user for authenticated endpoints, per-IP for login.",
    }
    return jsonify(limits)


@app.errorhandler(404)
def not_found(e):"""

# ─────────────────────────────────────────────────────────────────────────────
# Patch engine
# ─────────────────────────────────────────────────────────────────────────────

PATCHES = [
    ("Limiter imports",          OLD_JWT_IMPORT,       NEW_JWT_IMPORT),
    ("Limiter init",             OLD_JWT_INIT,         FIXED_JWT_INIT),
    ("Rate limit status route",  OLD_BEFORE_ERRORS,    NEW_BEFORE_ERRORS),
    ("429 error handler",        OLD_ERROR_HANDLERS,   NEW_ERROR_HANDLERS),
    ("Login rate limit",         OLD_LOGIN_ROUTE,      NEW_LOGIN_ROUTE),
    ("/ask rate limit",          OLD_ASK_ROUTE,        NEW_ASK_ROUTE),
    ("/execute_approved limit",  OLD_EXEC_ROUTE,       NEW_EXEC_ROUTE),
    ("/tools/<n>/run limit",     OLD_TOOL_RUN_ROUTE,   NEW_TOOL_RUN_ROUTE),
    ("/react/run limit",         OLD_REACT_ROUTE,      NEW_REACT_ROUTE),
]


def apply_patches():
    print("=" * 60)
    print("AVA — Day 6 Rate Limiting Patch")
    print(f"Target: {MAIN_FILE}")
    print("=" * 60)

    if not os.path.exists(MAIN_FILE):
        print(f"\n❌  FATAL: {MAIN_FILE} not found.")
        sys.exit(1)

    backup = MAIN_FILE + f".backup_day6_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(MAIN_FILE, backup)
    print(f"\n✅  Backup: {backup}")

    with open(MAIN_FILE) as f:
        content = f.read()

    failed = []
    for name, old, new in PATCHES:
        if old not in content:
            failed.append(name)
            print(f"  ⚠️  SKIP (anchor not found): {name}")
            continue
        content = content.replace(old, new, 1)
        print(f"  ✅  {name}")

    with open(MAIN_FILE, "w") as f:
        f.write(content)
    print(f"\n✅  Patched: {MAIN_FILE}")

    if failed:
        print(f"\n⚠️  {len(failed)} patch(es) skipped:")
        for name in failed:
            print(f"    - {name}")

    print("""
════════════════════════════════════════════════════════════

  Day 6 Rate Limiting Patch Complete!

  NEXT STEPS:
  ──────────────────────────────────────────────────────────
  1. Install:
       pip install flask-limiter

  2. Restart AVA:
       fuser -k 5002/tcp && sleep 1
       set -a && source .env && set +a
       python3 web_agent_v2.1_guardrail.py

  3. Run test suite:
       python3 test_day6.py

════════════════════════════════════════════════════════════
""")


if __name__ == "__main__":
    apply_patches()
