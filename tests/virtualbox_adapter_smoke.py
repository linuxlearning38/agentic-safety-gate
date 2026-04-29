#!/usr/bin/env python3
"""Smoke checks for the v2 VirtualBox adapter scaffold."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provisioning.adapters.virtualbox import (  # noqa: E402
    VirtualBoxAdapter,
    _parse_showvminfo_machine_readable,
)


def check(name: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    return condition


def main() -> int:
    adapter = VirtualBoxAdapter()
    failures: list[bool] = []

    plan = adapter.plan_instance(
        {
            "provider": "virtualbox",
            "os": "ubuntu",
            "role": "web_server",
            "cpu": 2,
            "ram_gb": 4,
            "disk_gb": 40,
            "network_mode": "nat",
            "firewall_profile": "web_public",
            "hardening_profile": "baseline_linux",
        }
    )

    parsed = _parse_showvminfo_machine_readable(
        '\n'.join(
            [
                'name="ava-web-server-001"',
                'VMState="running"',
                'ostype="Ubuntu_64"',
            ]
        )
    )

    failures.extend(
        [
            check("plan uses virtualbox provider", plan.provider == "virtualbox"),
            check("plan uses ubuntu cloud image placeholder", plan.image == "ubuntu-cloud-image"),
            check("plan converts RAM GB to MB", plan.memory_mb == 4096),
            check("plan preserves disk size", plan.disk_gb == 40),
            check("plan preserves network mode", plan.network_mode == "nat"),
            check("parsed showvminfo extracts VM state", parsed.get("VMState") == "running"),
        ]
    )

    try:
        adapter.plan_instance(
            {
                "os": "ubuntu",
                "role": "web_server",
                "cpu": 2,
                "ram_gb": 4,
                "disk_gb": 40,
                "network_mode": "invalid-mode",
            }
        )
        failures.append(check("invalid network mode is rejected", False))
    except ValueError:
        failures.append(check("invalid network mode is rejected", True))

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nVirtualBox adapter smoke failed: {failed} issue(s)")
        return 1
    print("\nVirtualBox adapter smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
