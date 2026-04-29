"""VirtualBox provider adapter scaffold for AVA v2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import subprocess
from typing import Any, Dict

from .base import ConnectionInfo, ProviderAdapter, ProviderState, ProvisioningPlan


ALLOWED_NETWORK_MODES = {"nat", "bridged", "hostonly"}


def _slugify_vm_name(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return cleaned or "vm"


def _parse_showvminfo_machine_readable(output: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip().strip('"')
    return parsed


def _power_state_from_showvminfo(parsed: Dict[str, str]) -> str:
    state = (parsed.get("VMState") or "").strip().lower()
    if state == "running":
        return "running"
    if state in {"poweroff", "powered off"}:
        return "stopped"
    if state == "saved":
        return "saved"
    if state == "aborted":
        return "aborted"
    if state:
        return state
    return "unknown"


@dataclass(slots=True)
class VirtualBoxAdapter(ProviderAdapter):
    """VirtualBox adapter for the first v2 provider slice."""

    vboxmanage_binary: str = "VBoxManage"
    image_name: str = "ubuntu-cloud-image"
    vm_group: str = "/AVA"
    vm_name_prefix: str = "ava"

    provider_name: str = "virtualbox"

    def plan_instance(self, desired_state: Dict[str, Any]) -> ProvisioningPlan:
        cpu_count = int(desired_state.get("cpu") or desired_state.get("cpu_count") or 0)
        memory_gb = int(desired_state.get("ram") or desired_state.get("ram_gb") or 0)
        disk_gb = int(desired_state.get("disk") or desired_state.get("disk_gb") or 0)
        network_mode = str(desired_state.get("network_mode") or "").strip().lower()
        role = str(desired_state.get("role") or "").strip().lower()
        os_name = str(desired_state.get("os") or "").strip().lower()

        if role != "web_server":
            raise ValueError("VirtualBox v2.0.0 scaffold only supports role='web_server'")
        if os_name != "ubuntu":
            raise ValueError("VirtualBox v2.0.0 scaffold only supports os='ubuntu'")
        if cpu_count <= 0:
            raise ValueError("cpu_count must be a positive integer")
        if memory_gb <= 0:
            raise ValueError("ram_gb must be a positive integer")
        if disk_gb <= 0:
            raise ValueError("disk_gb must be a positive integer")
        if network_mode not in ALLOWED_NETWORK_MODES:
            raise ValueError(f"network_mode must be one of {sorted(ALLOWED_NETWORK_MODES)}")

        requested_name = str(desired_state.get("vm_name") or "").strip()
        if requested_name:
            vm_name = _slugify_vm_name(requested_name)
        else:
            timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            vm_name = f"{self.vm_name_prefix}-{_slugify_vm_name(role)}-{timestamp}"

        return ProvisioningPlan(
            provider=self.provider_name,
            vm_name=vm_name,
            image=self.image_name,
            cpu_count=cpu_count,
            memory_mb=memory_gb * 1024,
            disk_gb=disk_gb,
            network_mode=network_mode,
            metadata={
                "os": os_name,
                "role": role,
                "vm_group": self.vm_group,
                "firewall_profile": desired_state.get("firewall_profile"),
                "hardening_profile": desired_state.get("hardening_profile"),
            },
        )

    def create_instance(self, plan: ProvisioningPlan) -> str:
        raise NotImplementedError("Phase 1 scaffold only: create_instance implementation is pending.")

    def start_instance(self, instance_id: str) -> str:
        raise NotImplementedError("Phase 1 scaffold only: start_instance implementation is pending.")

    def stop_instance(self, instance_id: str) -> str:
        raise NotImplementedError("Phase 1 scaffold only: stop_instance implementation is pending.")

    def destroy_instance(self, instance_id: str) -> str:
        raise NotImplementedError("Phase 1 scaffold only: destroy_instance implementation is pending.")

    def configure_network(self, instance_id: str, network_spec: Dict[str, Any]) -> str:
        raise NotImplementedError("Phase 1 scaffold only: configure_network implementation is pending.")

    def inject_access(self, instance_id: str, access_spec: Dict[str, Any]) -> str:
        raise NotImplementedError("Phase 1 scaffold only: inject_access implementation is pending.")

    def get_instance_state(self, instance_id: str) -> ProviderState:
        proc = subprocess.run(
            [self.vboxmanage_binary, "showvminfo", instance_id, "--machinereadable"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            error_text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).lower()
            if "could not find a registered machine" in error_text or "not found" in error_text:
                return ProviderState(
                    instance_id=instance_id,
                    exists=False,
                    power_state="missing",
                    provider_status="not_found",
                    raw={},
                )
            raise RuntimeError(f"VBoxManage showvminfo failed: {(proc.stderr or proc.stdout).strip()}")

        parsed = _parse_showvminfo_machine_readable(proc.stdout)
        return ProviderState(
            instance_id=instance_id,
            exists=True,
            power_state=_power_state_from_showvminfo(parsed),
            provider_status=parsed.get("VMState", "unknown"),
            raw=parsed,
        )

    def get_connection_info(self, instance_id: str) -> ConnectionInfo:
        raise NotImplementedError("Phase 1 scaffold only: get_connection_info implementation is pending.")

