#!/usr/bin/env python3
"""AVA v2.0.0 live end-to-end release gate."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control import approval  # noqa: E402
from control.input_router import route_query  # noqa: E402
from provisioning.adapters.virtualbox import VirtualBoxAdapter  # noqa: E402
from provisioning.bootstrap import SSHConnection, SSHExecutor  # noqa: E402
from provisioning.conversation import SessionPhase  # noqa: E402
from provisioning.rollback import ProvisioningRollbackManager  # noqa: E402
from provisioning.roles.web_server import WebServerRole  # noqa: E402
from provisioning.serving import ProvisioningChatService  # noqa: E402
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
    gecos: AVA v2 e2e user
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


def _extract_backtick_value(label: str, text: str) -> str:
    match = re.search(rf"{re.escape(label)}:\s*`([^`]+)`", text)
    if not match:
        raise RuntimeError(f"Could not extract {label} from guided response")
    return match.group(1)


def _skip(message: str) -> int:
    print(f"[SKIP] {message}")
    return 2


def main() -> int:
    started = time.monotonic()
    max_wall_seconds = int(os.getenv("AVA_V2_E2E_MAX_SECONDS", "600"))
    vboxmanage = os.getenv("AVA_VBOXMANAGE_PATH") or shutil.which("VBoxManage") or r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
    ssh_binary = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
    ssh_keygen = shutil.which("ssh-keygen") or r"C:\Windows\System32\OpenSSH\ssh-keygen.exe"
    template_name = os.getenv("AVA_VBOX_TEMPLATE_NAME", "ubuntu-cloud-image").strip() or "ubuntu-cloud-image"
    vm_name = os.getenv("AVA_V2_E2E_VM_NAME", "").strip() or f"ava-v2-e2e-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    retain_vm = _env_flag("AVA_V2_E2E_RETAIN_VM", False)
    timeout_seconds = int(os.getenv("AVA_V2_E2E_TIMEOUT_SECONDS", "900"))

    if not Path(vboxmanage).exists():
        return _skip(f"VBoxManage not found at {vboxmanage}")
    if not Path(ssh_binary).exists():
        return _skip(f"ssh not found at {ssh_binary}")
    if not Path(ssh_keygen).exists():
        return _skip(f"ssh-keygen not found at {ssh_keygen}")

    adapter = VirtualBoxAdapter(vboxmanage_binary=vboxmanage, image_name=template_name)
    template_state = adapter.get_instance_state(template_name)
    if not template_state.exists:
        return _skip(f"VirtualBox template '{template_name}' is not registered.")

    old_queue = os.environ.get("APPROVAL_QUEUE_PATH")
    old_session_db = os.environ.get("PROVISIONING_SESSION_DB")
    created_vm: str | None = None
    work_dir = Path(tempfile.mkdtemp(prefix="ava-v2-e2e-"))
    os.environ["APPROVAL_QUEUE_PATH"] = str(work_dir / "approval_queue.json")
    os.environ["PROVISIONING_SESSION_DB"] = str(work_dir / "provisioning_sessions.sqlite3")

    try:
        service = ProvisioningChatService(work_dir / "provisioning_sessions.sqlite3")
        route = route_query("I want a web server in Ubuntu")
        start = service.handle("e2e-user", "I want a web server in Ubuntu", route_intent=route.intent)
        _check("guided flow started", start.handled and "cpu" in start.response.lower(), start.response.splitlines()[0])

        specs = service.handle("e2e-user", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        approval_id = specs.metadata["provisioning"]["approval_id"]
        session = service.sessions.list_active("e2e-user")[0]
        _check("approval queued", bool(approval_id), str(approval_id))
        _check("desired state ready", session.phase == SessionPhase.AWAITING_APPROVAL, session.phase.value)

        pending = service.handle("e2e-user", "continue provisioning", route_intent=None)
        _check("pending approval blocks credential", "temporary password" not in pending.response.lower(), pending.response.splitlines()[0])

        approval.update_status(approval_id, "approved")
        approved = service.handle("e2e-user", "continue provisioning", route_intent=None)
        username = _extract_backtick_value("Username", approved.response)
        temporary_password = _extract_backtick_value("Temporary password", approved.response)
        _check("temporary credential issued after approval", bool(username and temporary_password), username)

        desired_state = dict(service.sessions.list_active("e2e-user")[0].desired_state)
        desired_state.update({"vm_name": vm_name, "username": username})

        key_path = work_dir / "ava_v2_e2e_ed25519"
        known_hosts_path = work_dir / "known_hosts"
        seed_dir = work_dir / "seed"
        seed_iso = work_dir / "seed.iso"
        _run([ssh_keygen, "-t", "ed25519", "-N", "", "-f", str(key_path), "-C", f"ava-v2-e2e-{vm_name}"], timeout=30)
        public_key = (key_path.with_suffix(".pub")).read_text(encoding="utf-8").strip()
        _write_cloud_init_seed(seed_dir, vm_name, username, temporary_password, public_key)
        _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "new_cloud_init_seed_iso.ps1"),
                "-SeedDir",
                str(seed_dir),
                "-OutputIsoPath",
                str(seed_iso),
            ],
            timeout=60,
        )
        _check("cloud-init seed ISO created", seed_iso.exists() and seed_iso.stat().st_size > 0, str(seed_iso))

        rollback = ProvisioningRollbackManager(adapter)
        try:
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
            _check("cloud-init access injected", access_result == "cloud_init_seed_attached", access_result)
            connection = adapter.get_connection_info(created_vm)
            adapter.start_instance(created_vm)
            _check("instance started", adapter.get_instance_state(created_vm).power_state == "running", created_vm)
            _check("SSH TCP reachable", _wait_for_tcp(connection.host, connection.port, timeout_seconds), f"{connection.host}:{connection.port}")

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
            cloud_init_ok = bool(
                cloud_init
                and cloud_init.exit_code == 0
                and f"AVA_CLOUD_INIT_READY {vm_name}" in cloud_init.stdout
            )
            _check("first access confirmed by cloud-init marker", cloud_init_ok, (cloud_init.stdout if cloud_init else "").strip())
            if not cloud_init_ok:
                raise RuntimeError("first access marker was not confirmed")

            login = service.handle("e2e-user", "I logged in and changed the password", route_intent=None)
            _check("guided flow accepted first-login confirmation", "baseline_linux" in login.response, login.response.splitlines()[0])

            hardening = service.handle("e2e-user", "yes harden it", route_intent=None)
            session = service.sessions.list_active("e2e-user")[0]
            _check("guided flow reached bootstrapping checkpoint", session.phase == SessionPhase.BOOTSTRAPPING, session.phase.value)
            _check("hardening choice recorded", session.collected_answers.get("post_login_actions") == ["baseline_linux"], str(session.collected_answers))

            role = WebServerRole()
            results = role.bootstrap(executor)
            for result in results:
                detail = f"exit={result.exit_code} duration={result.duration_seconds}s failure={result.failure_class}"
                _check(f"bootstrap command: {result.command}", result.exit_code == 0, detail)
                if result.exit_code != 0:
                    raise RuntimeError(result.stderr or f"bootstrap command failed: {result.command}")

            http_port = connection.metadata.get("http_host_port")
            if not http_port:
                raise RuntimeError("HTTP host port was not configured")
            ok, detail = _wait_for_http_200(f"http://127.0.0.1:{http_port}/", timeout_seconds=120)
            _check("host HTTP 200 verified", ok, detail)
            if not ok:
                raise RuntimeError(f"HTTP verification failed: {detail}")

            verification_report = VerificationEngine(
                adapter,
                executor_factory=lambda _connection: executor,
            ).verify_web_server(created_vm)
            _check("verification engine passed", verification_report.passed, verification_report.status)
            if not verification_report.passed:
                raise RuntimeError("verification engine reported failure")

            store = ProvisioningStateStore(work_dir / "provisioning-state.sqlite3")
            record = store.save_verification(
                session_id=session.session_id,
                desired_state=desired_state,
                actual_state=adapter.get_instance_state(created_vm).raw,
                verification_report=verification_report,
            )
            loaded = store.get(created_vm)
            _check("state store recorded completion", bool(loaded and loaded.outcome == "completed"), record.instance_id)

            elapsed = time.monotonic() - started
            _check("e2e wall time under release gate", elapsed <= max_wall_seconds, f"{elapsed:.1f}s <= {max_wall_seconds}s")
            print("\nAVA v2.0.0 e2e release gate passed.")
            print(f"Session: {session.session_id}")
            print(f"VM name: {created_vm}")
            print(f"HTTP: http://127.0.0.1:{http_port}/")
            print(f"Elapsed: {elapsed:.1f}s")
            return 0
        except Exception as exc:
            failure_report = rollback.handle_failure(
                session_id=session.session_id,
                phase=session.phase.value,
                failed_step="v2_e2e",
                failure_class="e2e_failed",
                message=str(exc),
                instance_id=created_vm,
                retain_for_debug=retain_vm,
            )
            print(f"[FAIL] v2 e2e error :: {exc}")
            print(f"[ROLLBACK] {failure_report.rollback.status} :: {failure_report.rollback.evidence}")
            return 1
    finally:
        if created_vm and not retain_vm:
            try:
                state = adapter.get_instance_state(created_vm)
                if state.exists:
                    adapter.destroy_instance(created_vm)
                    print(f"[CLEANUP] destroyed {created_vm}")
            except Exception as cleanup_exc:
                print(f"[WARN] final cleanup failed for {created_vm}: {cleanup_exc}")
        if old_queue is None:
            os.environ.pop("APPROVAL_QUEUE_PATH", None)
        else:
            os.environ["APPROVAL_QUEUE_PATH"] = old_queue
        if old_session_db is None:
            os.environ.pop("PROVISIONING_SESSION_DB", None)
        else:
            os.environ["PROVISIONING_SESSION_DB"] = old_session_db
        if not retain_vm:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"Retained e2e work directory: {work_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
