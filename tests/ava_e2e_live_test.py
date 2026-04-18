#!/usr/bin/env python3
"""End-to-end live validation against a running AVA service."""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request


BASE_URL = "https://localhost:5443"
LOGIN_PAYLOAD = {"username": "admin", "password": "ava-admin-2026"}


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def post_json(path: str, payload: dict, token: str | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, context=_ctx(), timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def login() -> str:
    data = post_json("/auth/login", LOGIN_PAYLOAD)
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"login failed: {data}")
    return token


def check(name: str, query: str, token: str, *, expected_type: str, expected_success: bool | None = None,
          expected_approval: bool | None = None, expected_blocked: bool | None = None) -> bool:
    try:
        data = post_json("/ask", {"query": query}, token)
    except urllib.error.HTTPError as exc:
        print(f"[FAIL] {name}: HTTP {exc.code}")
        return False
    except Exception as exc:
        print(f"[FAIL] {name}: {exc}")
        return False

    result = data.get("result") or {}
    actual_type = data.get("type")
    success = result.get("success")
    approval = result.get("approval_required")
    blocked = result.get("blocked")

    ok = actual_type == expected_type
    if expected_success is not None:
        ok = ok and success == expected_success
    if expected_approval is not None:
        ok = ok and approval == expected_approval
    if expected_blocked is not None:
        ok = ok and blocked == expected_blocked

    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: {query!r}")
    print(f"       type={actual_type} success={success} approval_required={approval} blocked={blocked}")
    if not ok:
        print(f"       response={data.get('response')!r}")
    return ok


def main() -> int:
    token = login()
    checks = [
        check("low-risk natural language", "show disk usage", token, expected_type="command", expected_success=True),
        check("system verification", "verify my system", token, expected_type="command", expected_success=True),
        check("docker inspection", "check docker", token, expected_type="command", expected_success=True),
        check("running containers", "show running containers", token, expected_type="command", expected_success=True),
        check("low-risk explicit command", "run df -h", token, expected_type="command", expected_success=True),
        check("medium-risk natural language", "restart the pod nginx", token, expected_type="command", expected_approval=True),
        check("critical destructive command", "rm -rf /", token, expected_type="command", expected_blocked=True),
        check("knowledge query", "What is Kubernetes?", token, expected_type="knowledge"),
    ]
    passed = sum(1 for item in checks if item)
    failed = len(checks) - passed
    print(f"\nResult: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
