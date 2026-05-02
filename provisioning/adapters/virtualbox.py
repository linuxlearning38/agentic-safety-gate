"""VirtualBox provider adapter scaffold for AVA v2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import socket
import subprocess
from typing import Any, Dict

from .base import ConnectionInfo, ProviderAdapter, ProviderState, ProvisioningPlan


ALLOWED_NETWORK_MODES = {"nat", "bridged", "hostonly"}
SSH_PORT_RANGE = (2222, 2999)
HTTP_PORT_RANGE = (8080, 8999)
_MISSING_VM_MARKERS = (
    "could not find a registered machine",
    "not found",
    "vbox_e_object_not_found",
)
_DISK_EXTENSIONS = (".vdi", ".vmdk", ".vhd", ".qcow", ".img")


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


def _find_free_local_port(start: int, end: int) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free localhost port available in range {start}-{end}")


def _looks_like_missing_vm(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _MISSING_VM_MARKERS)


def _extract_primary_disk_path(parsed: Dict[str, str]) -> str | None:
    for value in parsed.values():
        candidate = str(value or "").strip().strip('"')
        lowered = candidate.lower()
        if lowered.endswith(_DISK_EXTENSIONS):
            return candidate
    return None


def _extract_nat_forward_port(parsed: Dict[str, str], rule_name: str) -> int | None:
    prefix = f'{rule_name},tcp,127.0.0.1,'
    for key, value in parsed.items():
        if not key.startswith("Forwarding("):
            continue
        raw = str(value or "").strip().strip('"')
        if raw.startswith(prefix):
            parts = raw.split(",")
            if len(parts) >= 4:
                try:
                    return int(parts[3])
                except ValueError:
                    return None
    return None


def _parse_medium_capacity_mb(output: str) -> int | None:
    match = re.search(r"Capacity:\s+(\d+)\s+MBytes", output or "", flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


@dataclass(slots=True)
class VirtualBoxAdapter(ProviderAdapter):
    """VirtualBox adapter for the first v2 provider slice."""

    vboxmanage_binary: str = "VBoxManage"
    image_name: str = "ubuntu-cloud-image"
    vm_group: str = "/AVA"
    vm_name_prefix: str = "ava"

    provider_name: str = "virtualbox"

    def _run_vboxmanage(self, *args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [self.vboxmanage_binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            combined = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"VBoxManage {' '.join(args)} failed: {combined}")
        return proc

    def _set_extradata(self, instance_id: str, key: str, value: str) -> None:
        self._run_vboxmanage("setextradata", instance_id, key, str(value))

    def _get_extradata(self, instance_id: str) -> Dict[str, str]:
        proc = self._run_vboxmanage("getextradata", instance_id, "enumerate")
        text = (proc.stdout or "").strip()
        if not text or "No extradata available" in text:
            return {}

        result: Dict[str, str] = {}
        for line in text.splitlines():
            raw = line.strip()
            if not raw.startswith("Key: "):
                continue
            try:
                key_part, value_part = raw.split(", Value: ", 1)
            except ValueError:
                continue
            key = key_part.replace("Key: ", "", 1).strip()
            result[key] = value_part.strip()
        return result

    def _ensure_seed_storage_controller(self, instance_id: str, controller_name: str) -> None:
        state = self.get_instance_state(instance_id)
        controller_values = {
            str(value).strip('"')
            for key, value in state.raw.items()
            if key.startswith("storagecontrollername")
        }
        if controller_name in controller_values:
            return
        self._run_vboxmanage(
            "storagectl",
            instance_id,
            "--name",
            controller_name,
            "--add",
            "sata",
            "--controller",
            "IntelAhci",
            "--portcount",
            "1",
        )

    def _get_disk_capacity_mb(self, disk_path: str) -> int | None:
        proc = self._run_vboxmanage("showmediuminfo", "disk", disk_path, timeout=30, check=False)
        if proc.returncode != 0:
            return None
        return _parse_medium_capacity_mb((proc.stdout or "") + "\n" + (proc.stderr or ""))

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
                "hostname": vm_name,
                "ssh_host_port": desired_state.get("ssh_host_port"),
                "http_host_port": desired_state.get("http_host_port"),
                "username": desired_state.get("username") or "ubuntu",
            },
        )

    def create_instance(self, plan: ProvisioningPlan) -> str:
        template_state = self.get_instance_state(plan.image)
        if not template_state.exists:
            raise RuntimeError(
                f"Ubuntu template '{plan.image}' is not registered in VirtualBox. "
                "Create or import the cloud-image template before provisioning."
            )

        existing_state = self.get_instance_state(plan.vm_name)
        if existing_state.exists:
            raise RuntimeError(f"VirtualBox VM '{plan.vm_name}' already exists")

        self._run_vboxmanage(
            "clonevm",
            plan.image,
            "--name",
            plan.vm_name,
            "--register",
            "--mode",
            "machine",
            "--groups",
            plan.metadata.get("vm_group") or self.vm_group,
        )
        self._run_vboxmanage(
            "modifyvm",
            plan.vm_name,
            "--memory",
            str(plan.memory_mb),
            "--cpus",
            str(plan.cpu_count),
        )

        clone_state = self.get_instance_state(plan.vm_name)
        disk_path = _extract_primary_disk_path(clone_state.raw)
        if disk_path:
            requested_mb = plan.disk_gb * 1024
            current_capacity_mb = self._get_disk_capacity_mb(disk_path)
            if current_capacity_mb is None or requested_mb > current_capacity_mb:
                self._run_vboxmanage(
                    "modifymedium",
                    "disk",
                    disk_path,
                    "--resize",
                    str(requested_mb),
                )

        self.configure_network(
            plan.vm_name,
            {
                "network_mode": plan.network_mode,
                "ssh_host_port": plan.metadata.get("ssh_host_port"),
                "http_host_port": plan.metadata.get("http_host_port"),
                "username": plan.metadata.get("username") or "ubuntu",
            },
        )
        return plan.vm_name

    def start_instance(self, instance_id: str) -> str:
        state = self.get_instance_state(instance_id)
        if not state.exists:
            raise RuntimeError(f"VirtualBox VM '{instance_id}' does not exist")
        if state.power_state == "running":
            return "running"
        self._run_vboxmanage("startvm", instance_id, "--type", "headless")
        return self.get_instance_state(instance_id).power_state

    def stop_instance(self, instance_id: str) -> str:
        state = self.get_instance_state(instance_id)
        if not state.exists:
            return "not_found"
        if state.power_state != "running":
            return state.power_state
        self._run_vboxmanage("controlvm", instance_id, "acpipowerbutton")
        return "stopping"

    def destroy_instance(self, instance_id: str) -> str:
        state = self.get_instance_state(instance_id)
        if not state.exists:
            return "not_found"
        if state.power_state == "running":
            self._run_vboxmanage("controlvm", instance_id, "poweroff")
        self._run_vboxmanage("unregistervm", instance_id, "--delete")
        return "destroyed"

    def configure_network(self, instance_id: str, network_spec: Dict[str, Any]) -> str:
        network_mode = str(network_spec.get("network_mode") or "nat").strip().lower()
        if network_mode not in ALLOWED_NETWORK_MODES:
            raise ValueError(f"network_mode must be one of {sorted(ALLOWED_NETWORK_MODES)}")

        if network_mode == "nat":
            ssh_host_port = int(network_spec.get("ssh_host_port") or _find_free_local_port(*SSH_PORT_RANGE))
            http_host_port = int(network_spec.get("http_host_port") or _find_free_local_port(*HTTP_PORT_RANGE))

            self._run_vboxmanage("modifyvm", instance_id, "--nic1", "nat")
            self._run_vboxmanage("modifyvm", instance_id, "--natpf1", "delete", "guestssh", check=False)
            self._run_vboxmanage("modifyvm", instance_id, "--natpf1", "delete", "webhttp", check=False)
            self._run_vboxmanage(
                "modifyvm",
                instance_id,
                "--natpf1",
                f"guestssh,tcp,127.0.0.1,{ssh_host_port},,22",
            )
            self._run_vboxmanage(
                "modifyvm",
                instance_id,
                "--natpf1",
                f"webhttp,tcp,127.0.0.1,{http_host_port},,80",
            )

            self._set_extradata(instance_id, "AVA:connection:network_mode", "nat")
            self._set_extradata(instance_id, "AVA:connection:host", "127.0.0.1")
            self._set_extradata(instance_id, "AVA:connection:ssh_port", str(ssh_host_port))
            self._set_extradata(instance_id, "AVA:connection:http_port", str(http_host_port))
            self._set_extradata(instance_id, "AVA:connection:username", str(network_spec.get("username") or "ubuntu"))
            return "nat_configured"

        if network_mode == "bridged":
            adapter_name = str(network_spec.get("adapter_name") or "").strip()
            if not adapter_name:
                raise ValueError("bridged networking requires adapter_name")
            self._run_vboxmanage("modifyvm", instance_id, "--nic1", "bridged", "--bridgeadapter1", adapter_name)
            self._set_extradata(instance_id, "AVA:connection:network_mode", "bridged")
            return "bridged_configured"

        adapter_name = str(network_spec.get("adapter_name") or "").strip()
        if not adapter_name:
            raise ValueError("hostonly networking requires adapter_name")
        self._run_vboxmanage("modifyvm", instance_id, "--nic1", "hostonly", "--hostonlyadapter1", adapter_name)
        self._set_extradata(instance_id, "AVA:connection:network_mode", "hostonly")
        return "hostonly_configured"

    def inject_access(self, instance_id: str, access_spec: Dict[str, Any]) -> str:
        seed_iso_path = str(access_spec.get("seed_iso_path") or "").strip()
        if not seed_iso_path:
            raise ValueError("VirtualBox inject_access requires seed_iso_path")

        state = self.get_instance_state(instance_id)
        if not state.exists:
            raise RuntimeError(f"VirtualBox VM '{instance_id}' does not exist")
        if state.power_state == "running":
            raise RuntimeError("cloud-init seed media must be attached before the VM is started")

        controller_name = str(access_spec.get("storage_controller") or "AVA-Seed").strip() or "AVA-Seed"
        self._ensure_seed_storage_controller(instance_id, controller_name)
        self._run_vboxmanage(
            "storageattach",
            instance_id,
            "--storagectl",
            controller_name,
            "--port",
            "0",
            "--device",
            "0",
            "--type",
            "dvddrive",
            "--medium",
            seed_iso_path,
            "--tempeject",
            "on",
        )

        username = str(access_spec.get("username") or "ubuntu").strip() or "ubuntu"
        self._set_extradata(instance_id, "AVA:access:seed_iso_path", seed_iso_path)
        self._set_extradata(instance_id, "AVA:access:seed_controller", controller_name)
        self._set_extradata(instance_id, "AVA:connection:username", username)
        if access_spec.get("temporary_password"):
            self._set_extradata(instance_id, "AVA:access:temporary_password_present", "true")
        return "cloud_init_seed_attached"

    def get_instance_state(self, instance_id: str) -> ProviderState:
        proc = subprocess.run(
            [self.vboxmanage_binary, "showvminfo", instance_id, "--machinereadable"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            error_text = (proc.stderr or "") + "\n" + (proc.stdout or "")
            if _looks_like_missing_vm(error_text):
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
        state = self.get_instance_state(instance_id)
        if not state.exists:
            raise RuntimeError(f"VirtualBox VM '{instance_id}' does not exist")

        extradata = self._get_extradata(instance_id)
        host = extradata.get("AVA:connection:host") or "127.0.0.1"
        username = extradata.get("AVA:connection:username") or "ubuntu"

        ssh_port = extradata.get("AVA:connection:ssh_port")
        if ssh_port:
            port = int(ssh_port)
        else:
            port = _extract_nat_forward_port(state.raw, "guestssh") or 22

        http_port = extradata.get("AVA:connection:http_port")
        if http_port is None:
            detected_http_port = _extract_nat_forward_port(state.raw, "webhttp")
            http_port = str(detected_http_port) if detected_http_port else None

        return ConnectionInfo(
            username=username,
            host=host,
            port=port,
            temporary_password=None,
            metadata={
                "network_mode": extradata.get("AVA:connection:network_mode") or state.raw.get("nic1", "nat"),
                "http_host_port": int(http_port) if http_port else None,
                "seed_iso_attached": bool(extradata.get("AVA:access:seed_iso_path")),
            },
        )
