#!/usr/bin/env python3
"""Regression checks for OPA-backed AVA action decisions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def check(name: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    return condition


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def main() -> int:
    os.environ["AVA_OPA_ACTION_POLICY_ENABLED"] = "true"

    import control.secure_executor as secure_executor

    original_urlopen = secure_executor.urllib.request.urlopen
    original_opa_decision = secure_executor._opa_action_decision

    checks: list[bool] = []
    try:
        secure_executor.urllib.request.urlopen = lambda req, timeout=2: FakeResponse(
            {"result": {"effect": "allow", "reason": "test allow", "policy_id": "test"}}
        )
        decision = secure_executor._opa_action_decision(mode="command", risk="low", command="df -h", query="show disk")
        checks.append(check("OPA allow decision is parsed", decision["effect"] == "allow" and decision["policy_id"] == "test"))

        secure_executor.urllib.request.urlopen = lambda req, timeout=2: (_ for _ in ()).throw(OSError("opa down"))
        decision = secure_executor._opa_action_decision(mode="command", risk="low", command="df -h", query="show disk")
        checks.append(check("OPA unavailable fails closed", decision["effect"] == "block" and "unavailable" in decision["reason"]))

        secure_executor._opa_action_decision = lambda **kwargs: {
            "effect": "block",
            "reason": "test policy block",
            "policy_id": "test",
        }
        result = secure_executor.execute_command_secure("df -h", "show disk usage")
        checks.append(check("raw command execution respects OPA block", result["status"] == "blocked" and "test policy block" in result["reason"]))

    finally:
        secure_executor.urllib.request.urlopen = original_urlopen
        secure_executor._opa_action_decision = original_opa_decision

    failed = len([item for item in checks if not item])
    if failed:
        print(f"\nOPA action policy regression failed: {failed} issue(s)")
        return 1
    print("\nOPA action policy regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
