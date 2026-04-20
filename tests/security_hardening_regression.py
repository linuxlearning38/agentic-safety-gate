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


def main() -> int:
    app = _read(APP)
    compose = _read(COMPOSE)

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
            "hardened sensitive route errors do not return raw exception strings",
            "return jsonify({'error': str(e)}), 500" not in app
            and 'return jsonify({"error": str(e)}), 500' not in app
            and "'details': str(e)" not in app,
        ),
        check(
            "LLM generation errors do not expose raw exception text",
            "Error generating response: {str(e)}" not in app,
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
