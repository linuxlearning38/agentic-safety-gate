#!/usr/bin/env python3
"""Regression checks for concise host observability summaries."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control import tool_registry  # noqa: E402


def check(name: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    return condition


def _ok(output: str, metadata: dict | None = None) -> dict:
    return {"status": "success", "output": output, "metadata": metadata or {}}


def main() -> int:
    originals = {
        "_check_auth_events": tool_registry._check_auth_events,
        "_check_failed_services": tool_registry._check_failed_services,
        "_check_listening_ports": tool_registry._check_listening_ports,
        "_check_processes": tool_registry._check_processes,
        "_check_persistence_points": tool_registry._check_persistence_points,
        "_load_linux_operator_baseline": tool_registry._load_linux_operator_baseline,
        "_save_linux_operator_baseline": tool_registry._save_linux_operator_baseline,
        "_scan_host_vulnerabilities": tool_registry._scan_host_vulnerabilities,
        "_check_updates": tool_registry._check_updates,
        "_plan_next_diagnostic_step": tool_registry._plan_next_diagnostic_step,
        "_plan_safe_remediation": tool_registry._plan_safe_remediation,
    }

    failures: list[bool] = []
    try:
        tool_registry._check_auth_events = lambda _args: _ok("No recent auth failure markers found in available logs.")
        tool_registry._check_failed_services = lambda _args: _ok(
            "Failed service inspection is limited: host systemd is visible through the read-only host bridge.",
            {
                "failed_services": [],
                "failed_service_count": 0,
                "environment_note": "host_systemd_read_only",
            },
        )
        tool_registry._check_listening_ports = lambda _args: _ok(
            "LISTEN 0 2048 0.0.0.0:5443 0.0.0.0:* users:((\"gunicorn\",pid=8,fd=5))"
        )
        tool_registry._check_processes = lambda _args: _ok(
            "ava 8 0.1 1.1 gunicorn --bind 0.0.0.0:5443"
        )
        tool_registry._check_persistence_points = lambda _args: _ok("No unusual persistence points detected.")
        tool_registry._load_linux_operator_baseline = lambda: {}
        tool_registry._save_linux_operator_baseline = lambda _data: None
        tool_registry._scan_host_vulnerabilities = lambda _args: tool_registry._incomplete_vulnerability_scan_result(
            "parse_failed",
            "Could not parse Trivy JSON output: failed to download vulnerability DB",
        )
        tool_registry._check_updates = lambda _args: _ok("No pending package updates detected.")
        tool_registry._plan_next_diagnostic_step = lambda **_kwargs: {
            "step": "scan my system for vulnerabilities",
            "rationale": "The vulnerability scan did not complete and should be retried after the dependency issue is fixed.",
            "expected_signal": "A completed scan with a real CVE summary.",
        }
        tool_registry._plan_safe_remediation = lambda **_kwargs: None

        suspicious = tool_registry._check_suspicious_activity({})
        suspicious_output = suspicious.get("output", "").lower()
        suspicious_metadata = suspicious.get("metadata") or {}

        host_risk = tool_registry._assess_host_risk({})
        host_output = host_risk.get("output", "")

        failures.extend(
            [
                check("read-only failed-services limitation is not treated as a real alert", "failed systemd services detected" not in suspicious_output),
                check("suspicious output keeps the environment note", suspicious_metadata.get("failed_services_environment_note") == "host_systemd_read_only"),
                check("host-risk summary stays concise", "[Auth Events]" not in host_output and "[Top Processes]" not in host_output),
                check("host-risk summary keeps Trivy failure concise", "failed to download vulnerability DB" in host_output),
                check("host-risk summary avoids raw Trivy fatal dump", "mirror.gcr.io" not in host_output and "FATAL Fatal error" not in host_output),
            ]
        )
    finally:
        for name, original in originals.items():
            setattr(tool_registry, name, original)

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nHost observability regression failed: {failed} issue(s)")
        return 1
    print("\nHost observability regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
