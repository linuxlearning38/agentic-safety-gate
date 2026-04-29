#!/usr/bin/env python3
"""Live smoke test for the AVA v2 VirtualBox adapter.

This is intentionally opt-in. It expects:

- VirtualBox to be installed on the Windows host
- a registered Ubuntu template VM named ``ubuntu-cloud-image`` by default

Environment overrides:

- ``AVA_VBOXMANAGE_PATH``
- ``AVA_VBOX_TEMPLATE_NAME``
- ``AVA_VBOX_SMOKE_VM_NAME``
- ``AVA_VBOX_SMOKE_CPU``
- ``AVA_VBOX_SMOKE_RAM_GB``
- ``AVA_VBOX_SMOKE_DISK_GB``
- ``AVA_VBOX_SMOKE_NETWORK_MODE``
- ``AVA_VBOX_SMOKE_START_VM`` = ``true`` / ``false``
- ``AVA_VBOX_SMOKE_RETAIN_VM`` = ``true`` / ``false``
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provisioning.adapters.virtualbox import VirtualBoxAdapter  # noqa: E402


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


def main() -> int:
    vboxmanage = os.getenv("AVA_VBOXMANAGE_PATH") or shutil.which("VBoxManage") or r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
    template_name = os.getenv("AVA_VBOX_TEMPLATE_NAME", "ubuntu-cloud-image").strip() or "ubuntu-cloud-image"
    vm_name = os.getenv("AVA_VBOX_SMOKE_VM_NAME", "").strip() or f"ava-phase1-smoke-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    cpu = int(os.getenv("AVA_VBOX_SMOKE_CPU", "2"))
    ram_gb = int(os.getenv("AVA_VBOX_SMOKE_RAM_GB", "2"))
    disk_gb = int(os.getenv("AVA_VBOX_SMOKE_DISK_GB", "30"))
    network_mode = os.getenv("AVA_VBOX_SMOKE_NETWORK_MODE", "nat").strip().lower() or "nat"
    start_vm = _env_flag("AVA_VBOX_SMOKE_START_VM", False)
    retain_vm = _env_flag("AVA_VBOX_SMOKE_RETAIN_VM", False)

    adapter = VirtualBoxAdapter(vboxmanage_binary=vboxmanage, image_name=template_name)

    if not Path(vboxmanage).exists():
        print(f"[SKIP] VBoxManage not found at {vboxmanage}")
        return 2

    template_state = adapter.get_instance_state(template_name)
    if not template_state.exists:
        print(f"[SKIP] VirtualBox template '{template_name}' is not registered.")
        print("       Register the Ubuntu cloud-image template first, then rerun this smoke test.")
        return 2

    desired_state = {
        "provider": "virtualbox",
        "os": "ubuntu",
        "role": "web_server",
        "vm_name": vm_name,
        "cpu": cpu,
        "ram_gb": ram_gb,
        "disk_gb": disk_gb,
        "network_mode": network_mode,
        "firewall_profile": "web_public",
        "hardening_profile": "baseline_linux",
    }

    created_vm: str | None = None
    try:
        plan = adapter.plan_instance(desired_state)
        _check("plan built", plan.vm_name == vm_name, f"vm_name={plan.vm_name}")

        created_vm = adapter.create_instance(plan)
        _check("instance created", created_vm == vm_name, f"instance_id={created_vm}")

        state = adapter.get_instance_state(created_vm)
        _check("instance registered", state.exists, f"provider_status={state.provider_status}")

        connection = adapter.get_connection_info(created_vm)
        _check("SSH forwarding available", connection.host == "127.0.0.1" and connection.port > 0, f"{connection.host}:{connection.port}")
        _check(
            "HTTP forwarding available",
            bool(connection.metadata.get("http_host_port")),
            f"http_port={connection.metadata.get('http_host_port')}",
        )

        if start_vm:
            power_state = adapter.start_instance(created_vm)
            _check("instance start invoked", power_state in {"running", "starting", "poweringon"}, f"power_state={power_state}")

        print("\nLive VirtualBox adapter smoke passed.")
        print(f"VM name: {created_vm}")
        print(f"SSH: {connection.username}@{connection.host}:{connection.port}")
        print(f"HTTP host port: {connection.metadata.get('http_host_port')}")
        if retain_vm:
            print("Retain mode is enabled; VM was left registered for inspection.")
        return 0
    except Exception as exc:
        print(f"[FAIL] virtualbox live smoke error :: {exc}")
        return 1
    finally:
        if created_vm and not retain_vm:
            try:
                adapter.destroy_instance(created_vm)
                print(f"[CLEANUP] destroyed {created_vm}")
            except Exception as cleanup_exc:
                print(f"[WARN] cleanup failed for {created_vm}: {cleanup_exc}")


if __name__ == "__main__":
    raise SystemExit(main())
