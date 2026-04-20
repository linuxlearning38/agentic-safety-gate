#!/usr/bin/env python3
"""Regression checks for tamper-evident security audit logging."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def check(name: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    return condition


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        audit_path = os.path.join(tmp, "security_audit.json")
        os.environ["SECURITY_AUDIT_PATH"] = audit_path
        os.environ["AUDIT_INTEGRITY_KEY"] = "test-only-integrity-key"

        from control.security_layer import security_audit_log, verify_audit_log_integrity

        risk = {"risk": "low", "blast_radius": "read_only", "description": "test"}
        first = security_audit_log("executed", "df -h", "show disk usage", risk, [], "executed")
        second = security_audit_log("blocked", "rm -rf /", "rm -rf /", {"risk": "critical", "blast_radius": "system_wide"}, [], "blocked")

        import json

        with open(audit_path, "r", encoding="utf-8") as handle:
            entries = json.load(handle)

        checks = [
            check("audit entries are written", len(entries) == 2),
            check("entry hash is present", all(entry.get("entry_hash") for entry in entries)),
            check("previous hash links the chain", entries[1].get("prev_hash") == entries[0].get("entry_hash")),
            check("HMAC mode is used when key is configured", entries[0].get("integrity", {}).get("algorithm") == "hmac-sha256"),
            check("compatibility cmd field is present", first.get("cmd") == "df -h" and second.get("cmd") == "rm -rf /"),
            check("fresh chain verifies", verify_audit_log_integrity(entries).get("ok") is True),
        ]

        tampered = [dict(entry) for entry in entries]
        tampered[0]["command"] = "cat /etc/shadow"
        checks.append(check("tampered entry is detected", verify_audit_log_integrity(tampered).get("ok") is False))

    failed = len([item for item in checks if not item])
    if failed:
        print(f"\nAudit integrity regression failed: {failed} issue(s)")
        return 1
    print("\nAudit integrity regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
