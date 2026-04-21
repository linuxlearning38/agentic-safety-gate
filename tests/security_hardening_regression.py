#!/usr/bin/env python3
"""Static security regression checks for known pre-pen-test findings."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "web_agent_v2.1_guardrail.py"
COMPOSE = ROOT / "docker-compose.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check(name: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    return condition


def _route_block(source: str, route: str) -> str:
    pattern = re.compile(
        rf"@app\.route\('{re.escape(route)}'[^\n]*\)\n(?P<decorators>(?:@[^\n]+\n)*)def\s+[^\n]+\n(?P<body>.*?)(?=\n@app\.route|\n# HTML Template|\Z)",
        re.DOTALL,
    )
    match = pattern.search(source)
    if not match:
        return ""
    return match.group(0)


def _compose_service_block(source: str, service: str) -> str:
    pattern = re.compile(rf"^  {re.escape(service)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:|\Z)", re.DOTALL | re.MULTILINE)
    match = pattern.search(source)
    return match.group(0) if match else ""


def main() -> int:
    app = _read(APP)
    compose = _read(COMPOSE)
    ava_service = _compose_service_block(compose, "ava")

    checks = [
        check(
            "webhook secret has no known default fallback in app",
            'os.getenv("WEBHOOK_SECRET", "ava-webhook-2026")' not in app
            and "WEBHOOK_SECRET = os.getenv(\"WEBHOOK_SECRET\", \"\").strip()" in app,
        ),
        check(
            "webhook secret has no known default fallback in compose",
            "WEBHOOK_SECRET:      ${WEBHOOK_SECRET:-ava-webhook-2026}" not in compose
            and "WEBHOOK_SECRET:      ${WEBHOOK_SECRET:-}" in compose,
        ),
        check(
            "/security/stats requires admin auth",
            "@require_admin" in _route_block(app, "/security/stats"),
        ),
        check(
            "/security/audit requires admin auth",
            "@require_admin" in _route_block(app, "/security/audit"),
        ),
        check(
            "/security/posture requires admin auth and reports non-perfect zero trust",
            "@require_admin" in _route_block(app, "/security/posture")
            and '"perfect_zero_trust": False' in app
            and "remaining_gaps" in app,
        ),
        check(
            "hardened sensitive route errors do not return raw exception strings",
            "return jsonify({'error': str(e)}), 500" not in app
            and 'return jsonify({"error": str(e)}), 500' not in app
            and "'details': str(e)" not in app,
        ),
        check(
            "LLM generation errors do not expose raw exception text",
            "Error generating response: {str(e)}" not in app,
        ),
        check(
            "rate limiting uses shared Redis storage by default",
            'storage_uri=RATE_LIMIT_STORAGE_URI' in app
            and 'redis://redis:6379/0' in app
            and '"storage": RATE_LIMIT_STORAGE_URI' in app
            and 'storage_uri="memory://"' not in app,
        ),
        check(
            "internal dependency ports are bound to localhost only",
            '"127.0.0.1:6379:6379"' in compose
            and '"127.0.0.1:5432:5432"' in compose
            and '"127.0.0.1:8181:8181"' in compose
            and '"127.0.0.1:8200:8200"' in compose,
        ),
        check(
            "AVA does not mount the Docker socket directly",
            "- /var/run/docker.sock:/var/run/docker.sock" not in ava_service
            and "docker-socket-proxy:" in compose
            and "DOCKER_HOST:         http://docker-socket-proxy:2375" in compose,
        ),
        check(
            "AVA container has baseline confinement options",
            "no-new-privileges:true" in compose
            and "cap_drop:" in compose
            and "- ALL" in compose
            and "read_only: true" in compose
            and "/tmp:rw,noexec,nosuid" in compose,
        ),
        check(
            "runtime write paths stay inside explicit writable data boundary",
            'user: "999:999"' in compose
            and "DB_PATH:             /data/ava.db" in compose
            and "HISTORY_FILE:        /data/query_history.json" in compose
            and "AVA_DATA_DIR:        /data" in compose,
        ),
        check(
            "all mutable runtime artifacts are redirected away from read-only root",
            "APPROVAL_QUEUE_PATH: /data/approval_queue.json" in compose
            and "EXECUTION_LOG_PATH:  /data/execution_log.json" in compose
            and "SECURITY_AUDIT_PATH: /data/security_audit.json" in compose
            and "SECURITY_AUDIT_LOG:  /data/security_audit.json" in compose
            and "WHITELIST_PATH:      /data/control_whitelist.json" in compose
            and "AVA_REPORTS_DIR:     /data/ava_reports" in compose
            and 'AVA_TRIVY_SCAN_TIMEOUT_SECONDS: "45"' in compose
            and "LYNIS_LOG_PATH:      /tmp/lynis.log" in compose
            and "PYTHONDONTWRITEBYTECODE: \"1\"" in compose,
        ),
        check(
            "security posture reports read-only root as a runtime control",
            '"Container root filesystem is read-only"' in app
            and '"AVA_ROOT_READ_ONLY", "false"' in app
            and "AVA_ROOT_READ_ONLY:  \"true\"" in compose,
        ),
        check(
            "audit log integrity is exposed and tamper-evident",
            "verify_audit_log_integrity" in app
            and '"Audit log integrity is tamper-evident"' in app
            and "'audit_integrity': verify_audit_log_integrity(audit_log)" in app,
        ),
        check(
            "action execution is backed by OPA policy",
            "AVA_OPA_ACTION_POLICY_ENABLED: \"true\"" in compose
            and "OPA_ACTION_POLICY_URL: http://opa:8181/v1/data/ava/authz/decision" in compose
            and "ava_actions.rego" in "\n".join(path.name for path in (ROOT / "policies").glob("*.rego"))
            and "_opa_action_decision" in _read(ROOT / "control" / "secure_executor.py")
            and '"Action decisions pass through OPA"' in app,
        ),
        check(
            "signed command envelope foundation has no hardcoded secret",
            "AVA_COMMAND_SIGNING_KEY: ${AVA_COMMAND_SIGNING_KEY:-}" in compose
            and "AVA_COMMAND_REPLAY_CACHE_PATH: /data/command_replay_cache.json" in compose
            and "AVA_AGENT_IDENTITY_REGISTRY_PATH: /data/agent_identities.json" in compose
            and 'AVA_AGENT_MTLS_REQUIRED: "false"' in compose
            and '"Signed command envelope is available"' in app
            and '"Signed command replay cache is configured"' in app
            and '"Agent identity registry is configured"' in app
            and '"Remote-agent mTLS enforcement is active"' in app
            and '"command_signing_configured": command_signing_configured' in app
            and '"command_replay_cache_configured": command_replay_cache_configured' in app
            and '"agent_identity_registry_configured": agent_identity_registry_configured' in app
            and '"agent_mtls_required": agent_mtls_required' in app
            and "fleet-agent enforcement is not implemented" in app
            and "AVA_COMMAND_SIGNING_KEY" in _read(ROOT / "control" / "signed_commands.py")
            and "AVA_COMMAND_REPLAY_CACHE_PATH" in _read(ROOT / "control" / "signed_commands.py")
            and "consume_signed_command" in _read(ROOT / "control" / "signed_commands.py")
            and "CommandReplayCache" in _read(ROOT / "control" / "signed_commands.py")
            and "hmac.compare_digest" in _read(ROOT / "control" / "signed_commands.py")
            and "expires_at" in _read(ROOT / "control" / "signed_commands.py")
            and "signature" in _read(ROOT / "control" / "signed_commands.py")
            and "AgentIdentityRegistry" in _read(ROOT / "control" / "agent_identity.py")
            and "token_hash" in _read(ROOT / "control" / "agent_identity.py")
            and "cert_fingerprint" in _read(ROOT / "control" / "agent_identity.py")
            and "test-only-command-signing-key" not in _read(ROOT / "control" / "signed_commands.py"),
        ),
        check(
            "autonomous monitor/healer is opt-in for zero-trust mode",
            'AVA_MONITOR_ENABLED", "false"' in app
            and 'AVA_MONITOR_ENABLED: "false"' in compose,
        ),
    ]

    failed = len([item for item in checks if not item])
    if failed:
        print(f"\nSecurity hardening regression failed: {failed} issue(s)")
        return 1
    print("\nSecurity hardening regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
