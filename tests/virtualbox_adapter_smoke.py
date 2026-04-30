#!/usr/bin/env python3
"""Smoke checks for the v2 VirtualBox adapter scaffold."""

from __future__ import annotations

from dataclasses import dataclass
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provisioning.adapters import virtualbox as virtualbox_module  # noqa: E402
from provisioning.adapters.virtualbox import (  # noqa: E402
    VirtualBoxAdapter,
    _parse_showvminfo_machine_readable,
)


@dataclass
class FakeCompletedProcess:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def check(name: str, condition: bool) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    return condition


def fake_run_factory(command_log: list[list[str]]):
    vm_show_count = {"ava-web-server-001": 0}

    def fake_run(args: list[str], capture_output: bool, text: bool, timeout: int) -> FakeCompletedProcess:
        command_log.append(list(args))
        command = " ".join(args)

        if "showvminfo ubuntu-cloud-image --machinereadable" in command:
            return FakeCompletedProcess(
                stdout='\n'.join(
                    [
                        'name="ubuntu-cloud-image"',
                        'VMState="poweroff"',
                    ]
                )
            )

        if "showvminfo ava-web-server-001 --machinereadable" in command:
            vm_show_count["ava-web-server-001"] += 1
            if vm_show_count["ava-web-server-001"] == 1:
                return FakeCompletedProcess(returncode=1, stderr="Could not find a registered machine named 'ava-web-server-001'")
            return FakeCompletedProcess(
                stdout='\n'.join(
                    [
                        'name="ava-web-server-001"',
                        'VMState="running"',
                        'SATA-ImageUUID-0-0="{disk-uuid}"',
                        'SATA-0-0="C:\\\\VirtualBox\\\\ava-web-server-001.vdi"',
                        'Forwarding(0)="guestssh,tcp,127.0.0.1,2222,,22"',
                        'Forwarding(1)="webhttp,tcp,127.0.0.1,8080,,80"',
                    ]
                )
            )

        if "showvminfo missing-vm --machinereadable" in command:
            return FakeCompletedProcess(returncode=1, stderr="Could not find a registered machine named 'missing-vm'")

        if "showvminfo ava-seed-vm --machinereadable" in command:
            return FakeCompletedProcess(
                stdout="\n".join(
                    [
                        'name="ava-seed-vm"',
                        'VMState="poweroff"',
                    ]
                )
            )

        if "getextradata ava-web-server-001 enumerate" in command:
            return FakeCompletedProcess(
                stdout="\n".join(
                    [
                        "Key: AVA:connection:network_mode, Value: nat",
                        "Key: AVA:connection:host, Value: 127.0.0.1",
                        "Key: AVA:connection:ssh_port, Value: 2222",
                        "Key: AVA:connection:http_port, Value: 8080",
                        "Key: AVA:connection:username, Value: ubuntu",
                        "Key: AVA:access:seed_iso_path, Value: C:\\\\tmp\\\\seed.iso",
                    ]
                )
            )

        return FakeCompletedProcess(stdout="")

    return fake_run


def main() -> int:
    adapter = VirtualBoxAdapter()
    failures: list[bool] = []

    plan = adapter.plan_instance(
        {
            "provider": "virtualbox",
            "os": "ubuntu",
            "role": "web_server",
            "vm_name": "ava-web-server-001",
            "cpu": 2,
            "ram_gb": 4,
            "disk_gb": 40,
            "network_mode": "nat",
            "firewall_profile": "web_public",
            "hardening_profile": "baseline_linux",
            "ssh_host_port": 2222,
            "http_host_port": 8080,
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
            check("plan preserves requested SSH host port", plan.metadata.get("ssh_host_port") == 2222),
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

    original_run = virtualbox_module.subprocess.run
    command_log: list[list[str]] = []
    virtualbox_module.subprocess.run = fake_run_factory(command_log)
    try:
        adapter.create_instance(plan)
        failures.extend(
            [
                check(
                    "create_instance clones the ubuntu template",
                    any(cmd[1:4] == ["clonevm", "ubuntu-cloud-image", "--name"] for cmd in command_log),
                ),
                check(
                    "create_instance resizes the primary disk",
                    any(cmd[1:3] == ["modifymedium", "disk"] and "ava-web-server-001.vdi" in " ".join(cmd) for cmd in command_log),
                ),
                check(
                    "create_instance configures NAT SSH forwarding",
                    any("guestssh,tcp,127.0.0.1,2222,,22" in " ".join(cmd) for cmd in command_log),
                ),
                check(
                    "create_instance configures NAT HTTP forwarding",
                    any("webhttp,tcp,127.0.0.1,8080,,80" in " ".join(cmd) for cmd in command_log),
                ),
            ]
        )

        connection_info = adapter.get_connection_info("ava-web-server-001")
        failures.extend(
            [
                check("connection info returns localhost host", connection_info.host == "127.0.0.1"),
                check("connection info returns SSH port", connection_info.port == 2222),
                check("connection info exposes HTTP host port metadata", connection_info.metadata.get("http_host_port") == 8080),
                check("connection info exposes seed attachment metadata", connection_info.metadata.get("seed_iso_attached") is True),
            ]
        )

        access_state = adapter.inject_access(
            "ava-seed-vm",
            {
                "seed_iso_path": r"C:\tmp\seed.iso",
                "username": "avaadmin",
                "temporary_password": "present-but-not-returned",
            },
        )
        failures.extend(
            [
                check("inject_access reports seed attachment", access_state == "cloud_init_seed_attached"),
                check(
                    "inject_access creates seed storage controller",
                    any(cmd[1:5] == ["storagectl", "ava-seed-vm", "--name", "AVA-Seed"] for cmd in command_log),
                ),
                check(
                    "inject_access attaches seed ISO",
                    any(cmd[1:4] == ["storageattach", "ava-seed-vm", "--storagectl"] and r"C:\tmp\seed.iso" in cmd for cmd in command_log),
                ),
                check(
                    "inject_access records seed extradata",
                    any(cmd[1:4] == ["setextradata", "ava-seed-vm", "AVA:access:seed_iso_path"] for cmd in command_log),
                ),
            ]
        )

        stop_state = adapter.stop_instance("ava-web-server-001")
        destroy_state = adapter.destroy_instance("ava-web-server-001")
        failures.extend(
            [
                check("stop_instance returns stopping state for running VM", stop_state == "stopping"),
                check(
                    "stop_instance sends ACPI shutdown",
                    any(cmd[1:4] == ["controlvm", "ava-web-server-001", "acpipowerbutton"] for cmd in command_log),
                ),
                check("destroy_instance returns destroyed state", destroy_state == "destroyed"),
                check(
                    "destroy_instance powers off before unregistering",
                    any(cmd[1:4] == ["controlvm", "ava-web-server-001", "poweroff"] for cmd in command_log),
                ),
                check(
                    "destroy_instance unregisters and deletes the VM",
                    any(cmd[1:5] == ["unregistervm", "ava-web-server-001", "--delete"] for cmd in command_log),
                ),
            ]
        )

        missing_state = adapter.get_instance_state("missing-vm")
        failures.append(check("missing VM resolves to not_found state", missing_state.provider_status == "not_found"))
    finally:
        virtualbox_module.subprocess.run = original_run

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nVirtualBox adapter smoke failed: {failed} issue(s)")
        return 1
    print("\nVirtualBox adapter smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
