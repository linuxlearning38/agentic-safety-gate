"""
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
        logger.warning(f"[Auth] Login attempt for unknown user: '{username}'")
        return None

    stored_hash = user.get("password_hash", "").encode()
    try:
        if bcrypt.checkpw(password.encode("utf-8"), stored_hash):
            role = user.get("role", "readonly")
            logger.info(f"[Auth] Login success: user='{username}' role='{role}'")
            return {"username": username, "role": role}
    except Exception as e:
        logger.error(f"[Auth] bcrypt verification failed for '{username}': {e}")

    logger.warning(f"[Auth] Login failed: wrong password for user='{username}'")
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
    Decorator: JWT required AND role must be 'admin'.
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
                f"[Auth] Permission denied: user='{user}' role='{role}' "
                f"attempted admin-only endpoint '{fn.__name__}'"
            )
            return jsonify({
                "error":     "Admin access required for this operation.",
                "code":      "insufficient_permissions",
                "your_role": role,
                "required":  "admin"
            }), 403

        return fn(*args, **kwargs)
    return wrapper
