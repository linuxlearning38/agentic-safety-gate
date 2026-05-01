#!/usr/bin/env python3
"""Live Phase 4 smoke: clone Ubuntu, bootstrap nginx, verify HTTP 200."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provisioning.adapters.virtualbox import VirtualBoxAdapter  # noqa: E402
from provisioning.bootstrap import SSHConnection, SSHExecutor  # noqa: E402
from provisioning.roles.web_server import WebServerRole  # noqa: E402
from provisioning.state import ProvisioningStateStore  # noqa: E402
from provisioning.verify import VerificationEngine  # noqa: E402


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


def _run(command: list[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        combined = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"{' '.join(command)} failed: {combined}")
    return proc


def _wait_for_tcp(host: str, port: int, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                time.sleep(5)
    return False


def _wait_for_http_200(url: str, timeout_seconds: int) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=5) as response:
                status = getattr(response, "status", None) or response.getcode()
                if status == 200:
                    return True, f"HTTP {status}"
                last_error = f"HTTP {status}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(5)
    return False, last_error


def _wait_for_executor_command(executor: SSHExecutor, command: str, timeout_seconds: int):
    deadline = time.monotonic() + timeout_seconds
    last_result = None
    while time.monotonic() < deadline:
        last_result = executor.run(command, timeout_seconds=30)
        if last_result.exit_code == 0:
            return last_result
        time.sleep(8)
    return last_result


def _write_cloud_init_seed(seed_dir: Path, vm_name: str, username: str, password: str, public_key: str) -> None:
    seed_dir.mkdir(parents=True, exist_ok=True)
    marker = f"AVA_CLOUD_INIT_READY {vm_name}"
    user_data = f"""#cloud-config
preserve_hostname: false
hostname: {vm_name}
manage_etc_hosts: true
ssh_pwauth: true
disable_root: true
users:
  - default
  - name: {username}
    gecos: AVA v2 web smoke user
    groups: adm, sudo
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
    ssh_authorized_keys:
      - {public_key}
chpasswd:
  expire: false
  users:
    - name: {username}
      password: {password}
      type: text
write_files:
  - path: /var/tmp/ava-cloud-init-ready
    permissions: '0644'
    content: |
      {marker}
runcmd:
  - [ sh, -lc, "systemctl enable --now ssh || systemctl enable --now sshd || true" ]
"""
    meta_data = f"""instance-id: {vm_name}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}
local-hostname: {vm_name}
"""
    (seed_dir / "user-data").write_text(user_data, encoding="utf-8")
    (seed_dir / "meta-data").write_text(meta_data, encoding="utf-8")


def main() -> int:
    vboxmanage = os.getenv("AVA_VBOXMANAGE_PATH") or shutil.which("VBoxManage") or r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
    ssh_binary = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
    ssh_keygen = shutil.which("ssh-keygen") or r"C:\Windows\System32\OpenSSH\ssh-keygen.exe"
    template_name = os.getenv("AVA_VBOX_TEMPLATE_NAME", "ubuntu-cloud-image").strip() or "ubuntu-cloud-image"
    vm_name = os.getenv("AVA_VBOX_WEB_VM_NAME", "").strip() or f"ava-web-smoke-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    username = os.getenv("AVA_VBOX_WEB_USERNAME", "avaadmin").strip() or "avaadmin"
    temporary_password = os.getenv("AVA_VBOX_WEB_PASSWORD", "AvaTemp123!").strip() or "AvaTemp123!"
    retain_vm = _env_flag("AVA_VBOX_WEB_RETAIN_VM", False)
    timeout_seconds = int(os.getenv("AVA_VBOX_WEB_TIMEOUT_SECONDS", "900"))

    if not Path(vboxmanage).exists():
        print(f"[SKIP] VBoxManage not found at {vboxmanage}")
        return 2
    if not Path(ssh_binary).exists():
        print(f"[SKIP] ssh not found at {ssh_binary}")
        return 2
    if not Path(ssh_keygen).exists():
        print(f"[SKIP] ssh-keygen not found at {ssh_keygen}")
        return 2

    adapter = VirtualBoxAdapter(vboxmanage_binary=vboxmanage, image_name=template_name)
    template_state = adapter.get_instance_state(template_name)
    if not template_state.exists:
        print(f"[SKIP] VirtualBox template '{template_name}' is not registered.")
        return 2

    created_vm: str | None = None
    work_dir = Path(tempfile.mkdtemp(prefix="ava-web-smoke-"))
    try:
        key_path = work_dir / "ava_web_ed25519"
        known_hosts_path = work_dir / "known_hosts"
        seed_dir = work_dir / "seed"
        seed_iso = work_dir / "seed.iso"
        _run([ssh_keygen, "-t", "ed25519", "-N", "", "-f", str(key_path), "-C", f"ava-web-{vm_name}"], timeout=30)
        public_key = (key_path.with_suffix(".pub")).read_text(encoding="utf-8").strip()
        _write_cloud_init_seed(seed_dir, vm_name, username, temporary_password, public_key)

        seed_script = ROOT / "scripts" / "new_cloud_init_seed_iso.ps1"
        _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(seed_script),
                "-SeedDir",
                str(seed_dir),
                "-OutputIsoPath",
                str(seed_iso),
            ],
            timeout=60,
        )
        _check("cloud-init seed ISO created", seed_iso.exists() and seed_iso.stat().st_size > 0, str(seed_iso))

        desired_state = {
            "provider": "virtualbox",
            "os": "ubuntu",
            "role": "web_server",
            "vm_name": vm_name,
            "cpu": int(os.getenv("AVA_VBOX_WEB_CPU", "2")),
            "ram_gb": int(os.getenv("AVA_VBOX_WEB_RAM_GB", "2")),
            "disk_gb": int(os.getenv("AVA_VBOX_WEB_DISK_GB", "30")),
            "network_mode": "nat",
            "firewall_profile": "web_public",
            "hardening_profile": "baseline_linux",
            "username": username,
        }
        plan = adapter.plan_instance(desired_state)
        created_vm = adapter.create_instance(plan)
        _check("instance created", created_vm == vm_name, created_vm)
        access_result = adapter.inject_access(
            created_vm,
            {
                "seed_iso_path": str(seed_iso),
                "username": username,
                "temporary_password": temporary_password,
            },
        )
        _check("cloud-init seed attached", access_result == "cloud_init_seed_attached", access_result)

        connection = adapter.get_connection_info(created_vm)
        adapter.start_instance(created_vm)
        _check("instance started", adapter.get_instance_state(created_vm).power_state == "running", created_vm)
        _check("SSH TCP became reachable", _wait_for_tcp(connection.host, connection.port, timeout_seconds), f"{connection.host}:{connection.port}")

        executor = SSHExecutor(
            SSHConnection(
                host=connection.host,
                port=connection.port,
                username=username,
                private_key_path=str(key_path),
                known_hosts_path=str(known_hosts_path),
                ssh_binary=ssh_binary,
            )
        )
        cloud_init = _wait_for_executor_command(
            executor,
            "cloud-init status --wait >/tmp/ava-cloud-init-status.txt 2>&1; test -f /var/tmp/ava-cloud-init-ready; cat /var/tmp/ava-cloud-init-ready",
            timeout_seconds=timeout_seconds,
        )
        if cloud_init is None:
            raise RuntimeError("SSH command did not produce a result before timeout")
        cloud_init_ok = cloud_init.exit_code == 0 and f"AVA_CLOUD_INIT_READY {vm_name}" in cloud_init.stdout
        _check("cloud-init marker verified", cloud_init_ok, cloud_init.stdout.strip() or cloud_init.stderr.strip())
        if not cloud_init_ok:
            return 1

        role = WebServerRole()
        results = role.bootstrap(executor)
        for result in results:
            detail = f"exit={result.exit_code} duration={result.duration_seconds}s failure={result.failure_class}"
            _check(f"bootstrap command: {result.command}", result.exit_code == 0, detail)
            if result.exit_code != 0:
                print(result.stderr)
                return 1

        http_port = connection.metadata.get("http_host_port")
        if not http_port:
            raise RuntimeError("HTTP host port was not configured")
        ok, detail = _wait_for_http_200(f"http://127.0.0.1:{http_port}/", timeout_seconds=120)
        _check("host HTTP 200 verified", ok, detail)
        if not ok:
            return 1

        verification = VerificationEngine(
            adapter,
            executor_factory=lambda _connection: executor,
        )
        verification_report = verification.verify_web_server(created_vm)
        _check("verification engine passed", verification_report.passed, verification_report.status)
        if not verification_report.passed:
            for check in verification_report.checks:
                print(f"[VERIFY] {check.name}: {check.status} {check.evidence}")
            return 1

        store = ProvisioningStateStore(work_dir / "provisioning-state.sqlite3")
        record = store.save_verification(
            session_id=f"smoke-{created_vm}",
            desired_state=desired_state,
            actual_state=adapter.get_instance_state(created_vm).raw,
            verification_report=verification_report,
        )
        reloaded_record = store.get(created_vm)
        _check("verification state persisted", bool(reloaded_record and reloaded_record.outcome == "completed"), record.instance_id)

        print("\nVirtualBox web_server bootstrap smoke passed.")
        print(f"VM name: {created_vm}")
        print(f"SSH: {username}@{connection.host}:{connection.port}")
        print(f"HTTP: http://127.0.0.1:{http_port}/")
        if retain_vm:
            print("Retain mode is enabled; VM was left registered for inspection.")
        return 0
    except Exception as exc:
        print(f"[FAIL] virtualbox web bootstrap smoke error :: {exc}")
        return 1
    finally:
        if created_vm and not retain_vm:
            try:
                adapter.destroy_instance(created_vm)
                print(f"[CLEANUP] destroyed {created_vm}")
            except Exception as cleanup_exc:
                print(f"[WARN] cleanup failed for {created_vm}: {cleanup_exc}")
        if not retain_vm:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"Retained smoke work directory: {work_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
