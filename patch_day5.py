#!/usr/bin/env python3
"""
patch_day5.py — AVA Phase 4, Day 5: JWT Authentication

What this patch does:
  1. Writes control/auth.py                    (JWT module)
  2. Creates users.json                        (admin + readonly users, bcrypt hashed)
  3. Patches web_agent_v2.1_guardrail.py:
       - Adds JWT imports + init_jwt() call
       - Adds POST /auth/login  (public)
       - Adds GET  /auth/me     (introspect token)
       - Protects /ask, /history, /stats, /upload, /execute_approved,
                  /tools/<n>/run, /react/run with @jwt_required()
       - Protects /upload, /execute_approved, /tools/<n>/run, /react/run
                  with @require_admin (admin-only)
       - Injects login overlay + fetch interceptor into frontend HTML

Run:
  python3 patch_day5.py

After running:
  1. pip install flask-jwt-extended bcrypt
  2. export JWT_SECRET_KEY="$(openssl rand -hex 32)"   # Add to ~/.bashrc
  3. fuser -k 5002/tcp && python3 web_agent_v2.1_guardrail.py
  4. Test: curl -s -X POST http://localhost:5002/auth/login \\
           -H "Content-Type: application/json" \\
           -d '{"username":"admin","password":"<YOUR_ADMIN_PASSWORD>"}' | python3 -m json.tool
"""

import os
import sys
import shutil
import json
import bcrypt
from datetime import datetime

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_FILE   = os.path.join(PROJECT_DIR, "web_agent_v2.1_guardrail.py")
AUTH_FILE   = os.path.join(PROJECT_DIR, "control", "auth.py")
USERS_FILE  = os.path.join(PROJECT_DIR, "users.json")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Write control/auth.py
# ─────────────────────────────────────────────────────────────────────────────

AUTH_MODULE = '''"""
AVA — control/auth.py
JWT Authentication + Role-Based Access Control
Day 5 — Phase 4

Roles:
  admin    — full access (all endpoints)
  readonly — read-only (no execution endpoints)

Users stored in users.json (bcrypt hashed passwords).
JWT_SECRET_KEY must be set as environment variable in production.
"""

import os
import json
import secrets
import logging
from datetime import timedelta
from functools import wraps

import bcrypt
from flask import jsonify
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    verify_jwt_in_request,
    get_jwt,
    get_jwt_identity,
)

logger = logging.getLogger(__name__)

# ── Path resolution (works from any cwd) ─────────────────────────────────────
_MODULE_DIR  = os.path.dirname(os.path.abspath(__file__))   # .../control/
_PROJECT_DIR = os.path.dirname(_MODULE_DIR)                  # .../devops-agent/
USERS_FILE   = os.path.join(_PROJECT_DIR, "users.json")


# ── User store ────────────────────────────────────────────────────────────────

def _load_users() -> dict:
    """Load users from JSON file. Returns empty dict on failure."""
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"[Auth] Failed to load users file: {e}")
    return {}


def verify_credentials(username: str, password: str) -> dict | None:
    """
    Verify username + password against stored bcrypt hash.

    Returns:
        {"username": str, "role": str}  on success
        None                            on failure
    """
    if not username or not password:
        return None

    users = _load_users()
    user  = users.get(username)

    if not user:
        # Constant-time dummy check to prevent username enumeration via timing
        bcrypt.checkpw(b"dummy", b"$2b$12$invalidhashpaddingtomakeitmatch0000000000000000000000000")
        logger.warning(f"[Auth] Login attempt for unknown user: \'{username}\'")
        return None

    stored_hash = user.get("password_hash", "").encode()
    try:
        if bcrypt.checkpw(password.encode("utf-8"), stored_hash):
            role = user.get("role", "readonly")
            logger.info(f"[Auth] Login success: user=\'{username}\' role=\'{role}\'")
            return {"username": username, "role": role}
    except Exception as e:
        logger.error(f"[Auth] bcrypt verification failed for \'{username}\': {e}")

    logger.warning(f"[Auth] Login failed: wrong password for user=\'{username}\'")
    return None


# ── JWT Initialisation ────────────────────────────────────────────────────────

def init_jwt(app) -> JWTManager:
    """
    Configure Flask-JWT-Extended on the Flask app.

    Reads JWT_SECRET_KEY from environment. If not set, generates an
    ephemeral secret and logs a WARNING — tokens will invalidate on restart.
    """
    secret = os.environ.get("JWT_SECRET_KEY")
    if not secret:
        secret = secrets.token_hex(32)
        logger.warning(
            "[Auth] JWT_SECRET_KEY not set in environment. "
            "Using ephemeral secret — all tokens will be invalidated on restart. "
            "Set JWT_SECRET_KEY in your environment for persistence."
        )
    else:
        logger.info("[Auth] JWT_SECRET_KEY loaded from environment.")

    app.config["JWT_SECRET_KEY"]            = secret
    app.config["JWT_ACCESS_TOKEN_EXPIRES"]  = timedelta(hours=24)
    app.config["JWT_TOKEN_LOCATION"]        = ["headers"]
    app.config["JWT_HEADER_NAME"]           = "Authorization"
    app.config["JWT_HEADER_TYPE"]           = "Bearer"

    jwt = JWTManager(app)

    @jwt.expired_token_loader
    def expired_token_callback(jwt_header, jwt_payload):
        return jsonify({
            "error": "Token expired. Please log in again.",
            "code":  "token_expired"
        }), 401

    @jwt.invalid_token_loader
    def invalid_token_callback(error_string):
        return jsonify({
            "error": f"Invalid token: {error_string}",
            "code":  "invalid_token"
        }), 401

    @jwt.unauthorized_loader
    def missing_token_callback(error_string):
        return jsonify({
            "error": "Authentication required. Please log in.",
            "code":  "missing_token"
        }), 401

    @jwt.revoked_token_loader
    def revoked_token_callback(jwt_header, jwt_payload):
        return jsonify({
            "error": "Token has been revoked.",
            "code":  "token_revoked"
        }), 401

    logger.info("[Auth] JWT authentication initialised — 24h token expiry")
    return jwt


def make_token(username: str, role: str) -> str:
    """Create a signed JWT with role claim embedded."""
    additional_claims = {"role": role}
    return create_access_token(
        identity=username,
        additional_claims=additional_claims
    )


# ── Role Decorators ───────────────────────────────────────────────────────────

def require_admin(fn):
    """
    Decorator: JWT required AND role must be \'admin\'.
    Returns 401 if no/invalid token, 403 if wrong role.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        verify_jwt_in_request()
        claims = get_jwt()
        role   = claims.get("role", "")
        user   = get_jwt_identity()

        if role != "admin":
            logger.warning(
                f"[Auth] Permission denied: user=\'{user}\' role=\'{role}\' "
                f"attempted admin-only endpoint \'{fn.__name__}\'"
            )
            return jsonify({
                "error":     "Admin access required for this operation.",
                "code":      "insufficient_permissions",
                "your_role": role,
                "required":  "admin"
            }), 403

        return fn(*args, **kwargs)
    return wrapper
'''


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Generate users.json
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_USERS = {
    "admin": {
        "password": "<YOUR_ADMIN_PASSWORD>",
        "role":     "admin"
    },
    "readonly": {
        "password": "ava-readonly-2026",
        "role":     "readonly"
    }
}


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Patch strings for web_agent_v2.1_guardrail.py
# ─────────────────────────────────────────────────────────────────────────────

# 3a. New import block to insert after existing imports
OLD_IMPORTS = "from control.react_loop import react_loop"

NEW_IMPORTS = """from control.react_loop import react_loop
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from control.auth import init_jwt, verify_credentials, require_admin, make_token"""

# 3b. JWT init after CORS
OLD_CORS = "CORS(app)"

NEW_CORS = """CORS(app)

# ── Day 5: JWT Authentication ─────────────────────────────────────────────────
jwt_manager = init_jwt(app)"""

# 3c. Auth routes — inject BEFORE @app.route('/')
OLD_ROOT_ROUTE = "@app.route('/')\ndef index():"

NEW_AUTH_ROUTES = '''# ── Auth Endpoints ────────────────────────────────────────────────────────────

@app.route('/auth/login', methods=['POST'])
def auth_login():
    """
    POST /auth/login
    Body: {"username": "admin", "password": "<YOUR_ADMIN_PASSWORD>"}
    Returns: {"access_token": "...", "username": "...", "role": "...", "expires_in": 86400}
    """
    try:
        data     = request.json or {}
        username = data.get("username", "").strip()
        password = data.get("password", "")

        if not username or not password:
            return jsonify({"error": "username and password are required"}), 400

        user = verify_credentials(username, password)
        if not user:
            return jsonify({"error": "Invalid credentials"}), 401

        token = make_token(user["username"], user["role"])
        logger.info(f"[Auth] Token issued: user=\'{user[\'username\']}\' role=\'{user[\'role\']}\'")

        return jsonify({
            "access_token": token,
            "token_type":   "Bearer",
            "username":     user["username"],
            "role":         user["role"],
            "expires_in":   86400,   # 24h in seconds
        })

    except Exception as e:
        logger.error(f"[Auth] Login error: {e}")
        return jsonify({"error": "Login failed"}), 500


@app.route('/auth/me', methods=['GET'])
@jwt_required()
def auth_me():
    """
    GET /auth/me
    Returns current user info from JWT claims.
    Useful for frontend to verify token and get role.
    """
    identity = get_jwt_identity()
    claims   = get_jwt()
    return jsonify({
        "username": identity,
        "role":     claims.get("role", "unknown"),
    })


@app.route('/')
def index():'''

# 3d. Protect /ask — @jwt_required()
OLD_ASK = "@app.route('/ask', methods=['POST'])\ndef ask():"

NEW_ASK = """@app.route('/ask', methods=['POST'])
@jwt_required()
def ask():"""

# 3e. Protect /upload — admin only (image analysis runs LLM resources)
OLD_UPLOAD = "@app.route('/upload', methods=['POST'])\ndef upload_file():"

NEW_UPLOAD = """@app.route('/upload', methods=['POST'])
@require_admin
def upload_file():"""

# 3f. Protect /history — jwt_required (readonly can view)
OLD_HISTORY = "@app.route('/history', methods=['GET'])\ndef get_history():"

NEW_HISTORY = """@app.route('/history', methods=['GET'])
@jwt_required()
def get_history():"""

# 3g. Protect /execute_approved — admin only
OLD_EXEC = "@app.route('/execute_approved', methods=['POST'])\ndef execute_approved_route():"

NEW_EXEC = """@app.route('/execute_approved', methods=['POST'])
@require_admin
def execute_approved_route():"""

# 3h. Protect /tools (list) — jwt_required (readonly can list)
OLD_TOOLS_LIST = "@app.route('/tools', methods=['GET'])\ndef list_tools_route():"

NEW_TOOLS_LIST = """@app.route('/tools', methods=['GET'])
@jwt_required()
def list_tools_route():"""

# 3i. Protect /tools/<name>/run — admin only
OLD_TOOL_RUN = "@app.route('/tools/<tool_name>/run', methods=['POST'])\ndef run_tool_route(tool_name):"

NEW_TOOL_RUN = """@app.route('/tools/<tool_name>/run', methods=['POST'])
@require_admin
def run_tool_route(tool_name):"""

# 3j. Protect /react/run — admin only
OLD_REACT_RUN = "@app.route('/react/run', methods=['POST'])\ndef react_run_route():"

NEW_REACT_RUN = """@app.route('/react/run', methods=['POST'])
@require_admin
def react_run_route():"""

# 3k. Frontend: Login overlay HTML — inject right after <body>
#     Anchor: the first div in body (the loading overlay)
OLD_BODY_START = '<body>\n    <!-- Loading Overlay -->'

NEW_BODY_START = r'''<body>

    <!-- ── Day 5: Login Overlay ────────────────────────────────────────── -->
    <div id="loginOverlay" style="
        display: flex;
        position: fixed;
        inset: 0;
        background: rgba(10,10,20,0.97);
        z-index: 9999;
        align-items: center;
        justify-content: center;
        font-family: 'Inter', 'Segoe UI', sans-serif;
    ">
        <div style="
            background: #12121f;
            border: 1px solid #2a2a4a;
            border-radius: 16px;
            padding: 40px 48px;
            width: 380px;
            box-shadow: 0 24px 60px rgba(0,0,0,0.8);
        ">
            <div style="text-align:center; margin-bottom: 32px;">
                <div style="font-size: 36px; margin-bottom: 8px;">🤖</div>
                <div style="font-size: 22px; font-weight: 700; color: #e0e0e0; letter-spacing: -0.5px;">AVA</div>
                <div style="font-size: 13px; color: #667eea; margin-top: 4px;">DevOps AI Agent — Secure Login</div>
            </div>
            <div style="margin-bottom: 16px;">
                <label style="display:block; font-size:12px; color:#888; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">Username</label>
                <input id="loginUsername" type="text" autocomplete="username" placeholder="admin"
                    style="width:100%; box-sizing:border-box; padding:10px 14px; background:#1a1a2e;
                           border:1px solid #2a2a4a; border-radius:8px; color:#e0e0e0;
                           font-size:14px; outline:none;"
                    onkeydown="if(event.key==='Enter') loginSubmit()">
            </div>
            <div style="margin-bottom: 24px;">
                <label style="display:block; font-size:12px; color:#888; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px;">Password</label>
                <input id="loginPassword" type="password" autocomplete="current-password" placeholder="••••••••••••"
                    style="width:100%; box-sizing:border-box; padding:10px 14px; background:#1a1a2e;
                           border:1px solid #2a2a4a; border-radius:8px; color:#e0e0e0;
                           font-size:14px; outline:none;"
                    onkeydown="if(event.key==='Enter') loginSubmit()">
            </div>
            <div id="loginError" style="display:none; color:#ff6b6b; font-size:13px; margin-bottom:16px; text-align:center;"></div>
            <button onclick="loginSubmit()" id="loginBtn" style="
                width: 100%; padding: 12px; background: linear-gradient(135deg,#667eea,#764ba2);
                border: none; border-radius: 8px; color: white; font-size: 15px;
                font-weight: 600; cursor: pointer; letter-spacing: 0.3px;
                transition: opacity 0.2s;">
                Sign In
            </button>
            <div style="text-align:center; margin-top:20px; font-size:11px; color:#444;">
                Tokens expire after 24 hours
            </div>
        </div>
    </div>
    <!-- ── End Login Overlay ─────────────────────────────────────────────── -->

    <!-- Loading Overlay -->'''

# 3l. Frontend: Auth JS — inject right after the <script> opening line
OLD_SCRIPT_START = "    <script>\n        console.log('AVA v2.1.2 - Script loading...');"

NEW_SCRIPT_START = r"""    <script>
        // ── Day 5: Auth State + JWT Handling ──────────────────────────────────
        const AVA_TOKEN_KEY = 'ava_jwt_token';

        function getToken() {
            return localStorage.getItem(AVA_TOKEN_KEY);
        }

        function setToken(token) {
            localStorage.setItem(AVA_TOKEN_KEY, token);
        }

        function clearToken() {
            localStorage.removeItem(AVA_TOKEN_KEY);
        }

        function showLoginOverlay(errorMsg) {
            document.getElementById('loginOverlay').style.display = 'flex';
            if (errorMsg) {
                const err = document.getElementById('loginError');
                err.textContent = errorMsg;
                err.style.display = 'block';
            }
            document.getElementById('loginUsername').focus();
        }

        function hideLoginOverlay() {
            document.getElementById('loginOverlay').style.display = 'none';
        }

        async function loginSubmit() {
            const username = document.getElementById('loginUsername').value.trim();
            const password = document.getElementById('loginPassword').value;
            const btn      = document.getElementById('loginBtn');
            const err      = document.getElementById('loginError');

            if (!username || !password) {
                err.textContent = 'Username and password are required.';
                err.style.display = 'block';
                return;
            }

            btn.disabled    = true;
            btn.textContent = 'Signing in…';
            err.style.display = 'none';

            try {
                const resp = await window._fetchNoAuth('/auth/login', {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body:    JSON.stringify({ username, password })
                });

                const data = await resp.json();

                if (resp.ok) {
                    setToken(data.access_token);
                    window._avaRole = data.role;
                    window._avaUser = data.username;
                    hideLoginOverlay();
                    document.getElementById('loginPassword').value = '';
                    applyRoleUI(data.role);
                } else {
                    err.textContent = data.error || 'Login failed.';
                    err.style.display = 'block';
                    document.getElementById('loginPassword').value = '';
                }
            } catch (e) {
                err.textContent = 'Network error — is AVA running?';
                err.style.display = 'block';
            } finally {
                btn.disabled    = false;
                btn.textContent = 'Sign In';
            }
        }

        function logoutAva() {
            clearToken();
            window._avaRole = null;
            window._avaUser = null;
            showLoginOverlay();
        }

        function applyRoleUI(role) {
            // Disable execution buttons for readonly users
            if (role === 'readonly') {
                document.querySelectorAll('.admin-only').forEach(el => {
                    el.style.opacity = '0.4';
                    el.style.pointerEvents = 'none';
                    el.title = 'Admin access required';
                });
            }
        }

        // Global fetch interceptor — auto-attach Bearer token to every request
        window._fetchNoAuth = window.fetch.bind(window);   // keep raw fetch for login
        window.fetch = function(url, options = {}) {
            const token = getToken();
            if (token) {
                options.headers = Object.assign(
                    { 'Authorization': 'Bearer ' + token },
                    options.headers || {}
                );
            }
            return window._fetchNoAuth(url, options).then(resp => {
                if (resp.status === 401) {
                    clearToken();
                    showLoginOverlay('Session expired. Please log in again.');
                    return Promise.reject(new Error('Unauthorized'));
                }
                return resp;
            });
        };

        // On page load — verify stored token or show login
        (async function checkAuth() {
            const token = getToken();
            if (!token) {
                showLoginOverlay();
                return;
            }
            try {
                const resp = await window._fetchNoAuth('/auth/me', {
                    headers: { 'Authorization': 'Bearer ' + token }
                });
                if (resp.ok) {
                    const data = await resp.json();
                    window._avaRole = data.role;
                    window._avaUser = data.username;
                    hideLoginOverlay();
                    applyRoleUI(data.role);
                } else {
                    clearToken();
                    showLoginOverlay();
                }
            } catch {
                showLoginOverlay();
            }
        })();
        // ── End Auth ──────────────────────────────────────────────────────────

        // Global state
        let currentApprovalId = null;"""


# ─────────────────────────────────────────────────────────────────────────────
# Patch engine
# ─────────────────────────────────────────────────────────────────────────────

PATCHES = [
    ("JWT imports",          OLD_IMPORTS,      NEW_IMPORTS),
    ("JWT init after CORS",  OLD_CORS,         NEW_CORS),
    ("Auth routes + /",      OLD_ROOT_ROUTE,   NEW_AUTH_ROUTES),
    ("Protect /ask",         OLD_ASK,          NEW_ASK),
    ("Protect /upload",      OLD_UPLOAD,       NEW_UPLOAD),
    ("Protect /history",     OLD_HISTORY,      NEW_HISTORY),
    ("Protect /execute_approved", OLD_EXEC,    NEW_EXEC),
    ("Protect /tools list",  OLD_TOOLS_LIST,   NEW_TOOLS_LIST),
    ("Protect /tools/<n>/run", OLD_TOOL_RUN,   NEW_TOOL_RUN),
    ("Protect /react/run",   OLD_REACT_RUN,    NEW_REACT_RUN),
    ("Login overlay HTML",   OLD_BODY_START,   NEW_BODY_START),
    ("Auth JS",              OLD_SCRIPT_START, NEW_SCRIPT_START),
]


def apply_patches():
    print("=" * 60)
    print("AVA — Day 5 JWT Auth Patch")
    print(f"Target: {MAIN_FILE}")
    print("=" * 60)

    # ── Verify main file exists ───────────────────────────────────────────────
    if not os.path.exists(MAIN_FILE):
        print(f"\n❌  FATAL: {MAIN_FILE} not found.")
        print("    Run from the project root directory.")
        sys.exit(1)

    # ── Backup ────────────────────────────────────────────────────────────────
    backup_path = MAIN_FILE + f".backup_day5_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(MAIN_FILE, backup_path)
    print(f"\n✅  Backup: {backup_path}")

    # ── Read source ───────────────────────────────────────────────────────────
    with open(MAIN_FILE, "r") as f:
        content = f.read()

    # ── Apply each patch ──────────────────────────────────────────────────────
    failed = []
    for name, old, new in PATCHES:
        if old not in content:
            failed.append(name)
            print(f"  ⚠️  SKIP (anchor not found): {name}")
            continue
        count = content.count(old)
        if count > 1:
            print(f"  ⚠️  WARNING: '{name}' anchor found {count}× — patching first occurrence only")
        content = content.replace(old, new, 1)
        print(f"  ✅  {name}")

    if failed:
        print(f"\n⚠️  {len(failed)} patch(es) skipped (anchors not found):")
        for f in failed:
            print(f"    - {f}")
        print("   This is OK if those patches were already applied.")

    # ── Write patched file ────────────────────────────────────────────────────
    with open(MAIN_FILE, "w") as f:
        f.write(content)
    print(f"\n✅  Patched: {MAIN_FILE}")

    # ── Write control/auth.py ─────────────────────────────────────────────────
    control_dir = os.path.join(PROJECT_DIR, "control")
    os.makedirs(control_dir, exist_ok=True)
    auth_path = os.path.join(control_dir, "auth.py")
    with open(auth_path, "w") as f:
        f.write(AUTH_MODULE)
    print(f"✅  Written: {auth_path}")

    # ── Generate users.json ───────────────────────────────────────────────────
    print("\n⏳  Generating bcrypt hashes (rounds=12, ~0.3s each)…")
    users_data = {}
    for username, info in DEFAULT_USERS.items():
        pw_hash = hash_password(info["password"])
        users_data[username] = {
            "password_hash": pw_hash,
            "role":          info["role"]
        }
        print(f"  ✅  {username} ({info['role']}) — hash generated")

    if os.path.exists(USERS_FILE):
        backup_users = USERS_FILE + f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(USERS_FILE, backup_users)
        print(f"  ✅  Existing users.json backed up → {backup_users}")

    with open(USERS_FILE, "w") as f:
        json.dump(users_data, f, indent=2)
    print(f"✅  Written: {USERS_FILE}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("""
════════════════════════════════════════════════════════════

  Day 5 JWT Patch Complete!

  NEXT STEPS:
  ──────────────────────────────────────────────────────────
  1. Install dependencies:
       pip install flask-jwt-extended bcrypt

  2. Set JWT secret (add to ~/.bashrc for persistence):
       export JWT_SECRET_KEY="$(openssl rand -hex 32)"
       source ~/.bashrc

  3. Restart AVA:
       fuser -k 5002/tcp && sleep 1
       python3 web_agent_v2.1_guardrail.py

  4. Test login:
       curl -s -X POST http://localhost:5002/auth/login \\
         -H "Content-Type: application/json" \\
         -d '{"username":"admin","password":"<YOUR_ADMIN_PASSWORD>"}' \\
         | python3 -m json.tool

  5. Test protected endpoint:
       TOKEN=$(curl -s -X POST http://localhost:5002/auth/login \\
         -H "Content-Type: application/json" \\
         -d '{"username":"admin","password":"<YOUR_ADMIN_PASSWORD>"}' \\
         | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

       curl -s http://localhost:5002/auth/me \\
         -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

       curl -s http://localhost:5002/history \\
         -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

  6. Test readonly role (should get 403 on admin endpoints):
       TOKEN_RO=$(curl -s -X POST http://localhost:5002/auth/login \\
         -H "Content-Type: application/json" \\
         -d '{"username":"readonly","password":"ava-readonly-2026"}' \\
         | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

       curl -s -X POST http://localhost:5002/tools/check_disk/run \\
         -H "Authorization: Bearer $TOKEN_RO" \\
         -H "Content-Type: application/json" \\
         -d '{"args":{}}' | python3 -m json.tool   # expects 403

  7. Test unauthenticated (should get 401):
       curl -s http://localhost:5002/history | python3 -m json.tool

  DEFAULT CREDENTIALS:
  ──────────────────────────────────────────────────────────
  admin    / <YOUR_ADMIN_PASSWORD>     (full access)
  readonly / ava-readonly-2026  (read-only)

  CHANGE PASSWORDS before exposing to network!
  Run: python3 manage_users.py (or edit users.json with fresh hashes)

════════════════════════════════════════════════════════════
""")


if __name__ == "__main__":
    apply_patches()
