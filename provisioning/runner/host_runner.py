"""Windows host-side runner for AVA v2 chat-approved provisioning jobs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.request import urlopen

from provisioning.adapters.virtualbox import VirtualBoxAdapter
from provisioning.bootstrap import SSHConnection, SSHExecutor
from provisioning.rollback import ProvisioningRollbackManager
from provisioning.roles.web_server import WebServerRole
from provisioning.verify import VerificationEngine

from .job_queue import ConsoleSessionRequest, Day2OperationJob, Day2OperationResult, ProvisioningJob, RedisProvisioningJobQueue
from .result_writer import ProvisioningResultWriter


ROOT = Path(__file__).resolve().parents[2]


def _runner_code_fingerprint() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except OSError:
        return "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _run(command: list[str], *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        combined = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"{command[0]} failed: {combined}")
    return proc


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ps_array_literal(values: list[str]) -> str:
    return "@(" + ", ".join(_ps_single_quote(str(value)) for value in values) + ")"


def _powershell_start_process_command(file_path: str, argument_list: list[str]) -> list[str]:
    script = (
        "Start-Process "
        f"-FilePath {_ps_single_quote(file_path)} "
        f"-ArgumentList {_ps_array_literal(argument_list)}"
    )
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]


def _ssh_config_path_value(path: str | Path | None) -> str:
    """Quote OpenSSH config path values so Windows paths with spaces survive parsing."""
    if not path:
        return "NUL"
    normalized = str(Path(path)).replace("\\", "/")
    escaped = normalized.replace('"', '\\"')
    return f'"{escaped}"'


def _find_putty_binary() -> str | None:
    candidates = [
        os.getenv("AVA_PUTTY_PATH"),
        shutil.which("putty"),
        shutil.which("putty.exe"),
        r"C:\Program Files\PuTTY\putty.exe",
        r"C:\Program Files (x86)\PuTTY\putty.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def _windows_private_key_acl_command(path: Path) -> list[str]:
    path_literal = _ps_single_quote(str(path))
    script = "\n".join(
        [
            f"$path = {path_literal}",
            "$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User",
            "$acl = Get-Acl -LiteralPath $path",
            "$acl.SetOwner($current)",
            "$acl.SetAccessRuleProtection($true, $false)",
            "foreach ($rule in @($acl.Access)) { [void] $acl.RemoveAccessRuleAll($rule) }",
            "$rights = [System.Security.AccessControl.FileSystemRights]::Read",
            "$allow = [System.Security.AccessControl.AccessControlType]::Allow",
            "$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($current, $rights, $allow)",
            "$acl.AddAccessRule($rule)",
            "Set-Acl -LiteralPath $path -AclObject $acl",
        ]
    )
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]


def _lock_down_private_key(path: Path) -> None:
    """Keep OpenSSH happy by restricting runner private-key permissions."""
    if os.name == "nt":
        _run(_windows_private_key_acl_command(path), timeout=30, check=False)
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _provider_state_dict(state) -> dict[str, object]:
    """Return a small, serializable provider state for Day-2 evidence."""
    return {
        "instance_id": getattr(state, "instance_id", None),
        "exists": bool(getattr(state, "exists", False)),
        "power_state": getattr(state, "power_state", None),
        "provider_status": getattr(state, "provider_status", None),
        "raw": getattr(state, "raw", None),
    }


def _wait_for_tcp(host: str, port: int, timeout_seconds: int, *, heartbeat: Callable[[], None] | None = None) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if heartbeat:
            heartbeat()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                time.sleep(5)
    return False


def _wait_for_http_200(
    url: str,
    timeout_seconds: int,
    *,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        if heartbeat:
            heartbeat()
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


def _wait_for_executor_command(
    executor: SSHExecutor,
    command: str,
    timeout_seconds: int,
    *,
    redact: tuple[str, ...],
    heartbeat: Callable[[], None] | None = None,
    success_stdout_contains: str | None = None,
    attempt_timeout_seconds: int = 30,
):
    deadline = time.monotonic() + timeout_seconds
    last_result = None
    while time.monotonic() < deadline:
        if heartbeat:
            heartbeat()
        last_result = executor.run(command, timeout_seconds=attempt_timeout_seconds, redact_patterns=redact)
        if success_stdout_contains and success_stdout_contains in (last_result.stdout or ""):
            # The guest printed the agreed marker, so treat late SSH close
            # timeouts as successful readiness instead of failing the VM.
            return replace(last_result, exit_code=0, timed_out=False, failure_class=None)
        if last_result.exit_code == 0:
            return last_result
        time.sleep(8)
    return last_result


RECOVERABLE_GUEST_FAILURE_CLASSES = {
    "guest_readiness_timeout",
    "guest_network_not_ready",
    "guest_bootstrap_not_ready",
}


class RecoverableGuestReadinessError(RuntimeError):
    """A VM exists, but the guest OS did not become safely manageable in time."""


def _is_recoverable_guest_failure(failure_class: str) -> bool:
    return failure_class in RECOVERABLE_GUEST_FAILURE_CLASSES


def _write_cloud_init_seed(seed_dir: Path, vm_name: str, username: str, password: str, public_key: str) -> None:
    seed_dir.mkdir(parents=True, exist_ok=True)
    marker = f"AVA_CLOUD_INIT_READY {vm_name}"
    safe_vm_name = json.dumps(vm_name)
    safe_username = json.dumps(username)
    safe_password = json.dumps(password)
    safe_public_key = json.dumps(public_key)
    user_data = f"""#cloud-config
preserve_hostname: false
hostname: {safe_vm_name}
manage_etc_hosts: true
ssh_pwauth: true
disable_root: true
users:
  - default
  - name: {safe_username}
    gecos: AVA v2 provisioned user
    groups: adm, sudo
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
  - name: ava-runner
    gecos: AVA runner automation
    groups: adm, sudo
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - {safe_public_key}
chpasswd:
  expire: true
  users:
    - name: {safe_username}
      password: {safe_password}
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


def _split_nginx_log_output(stdout: str) -> tuple[str, str, str]:
    service_log, _, rest = (stdout or "").partition("--- AVA_ACCESS_LOG ---")
    access_log, _, error_log = rest.partition("--- AVA_ERROR_LOG ---")
    return service_log.strip(), access_log.strip(), error_log.strip()


@dataclass(slots=True)
class HostRunnerConfig:
    """Runtime config for the manually started v2.0.0 host runner."""

    vboxmanage: str
    ssh_binary: str
    ssh_keygen: str
    template_name: str
    work_root: Path
    log_path: Path
    retain_debug: bool = False
    timeout_seconds: int = 900
    poll_timeout_seconds: int = 30
    max_jobs: int | None = None

    @classmethod
    def from_env(cls) -> "HostRunnerConfig":
        work_root = Path(os.getenv("AVA_HOST_RUNNER_WORK_DIR", str(ROOT / ".ava-runner")))
        return cls(
            vboxmanage=os.getenv("AVA_VBOXMANAGE_PATH")
            or shutil.which("VBoxManage")
            or r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
            ssh_binary=shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe",
            ssh_keygen=shutil.which("ssh-keygen") or r"C:\Windows\System32\OpenSSH\ssh-keygen.exe",
            template_name=os.getenv("AVA_VBOX_TEMPLATE_NAME", "ubuntu-cloud-image").strip() or "ubuntu-cloud-image",
            work_root=work_root,
            log_path=Path(os.getenv("AVA_HOST_RUNNER_LOG_PATH", str(work_root / "host_runner.log"))),
            retain_debug=_env_flag("AVA_HOST_RUNNER_RETAIN_DEBUG", False),
            timeout_seconds=int(os.getenv("AVA_HOST_RUNNER_TIMEOUT_SECONDS", "900")),
            poll_timeout_seconds=int(os.getenv("AVA_HOST_RUNNER_POLL_TIMEOUT_SECONDS", "30")),
            max_jobs=int(os.getenv("AVA_HOST_RUNNER_MAX_JOBS", "0") or "0") or None,
        )


_ISO_UNLINK_ATTEMPTS = 20
_ISO_UNLINK_DELAY_SECONDS = 3.0


class HostRunner:
    """Consume approved Redis jobs and execute them through Windows VirtualBox."""

    def __init__(
        self,
        *,
        queue: RedisProvisioningJobQueue | None = None,
        config: HostRunnerConfig | None = None,
        logger: logging.Logger | None = None,
    ):
        self.queue = queue or RedisProvisioningJobQueue()
        self.writer = ProvisioningResultWriter(self.queue)
        self.config = config or HostRunnerConfig.from_env()
        self.adapter = VirtualBoxAdapter(
            vboxmanage_binary=self.config.vboxmanage,
            image_name=self.config.template_name,
        )
        self.logger = logger or _build_logger(self.config.log_path)
        self._console_threads: dict[str, threading.Thread] = {}

    def run_forever(self) -> None:
        processed = 0
        self.logger.info("AVA host runner started fingerprint=%s", _runner_code_fingerprint())
        while True:
            try:
                self._write_idle_heartbeat()
                if self.config.max_jobs is not None and processed >= self.config.max_jobs:
                    self.logger.info("Max jobs reached; exiting")
                    return
                console = self.queue.claim_next_console_session(timeout_seconds=1)
                if console is not None:
                    self._start_console_session(console)
                    continue
                operation = self.queue.claim_next_day2_operation(timeout_seconds=1)
                if operation is not None:
                    processed += 1
                    with self._day2_heartbeat_loop(operation):
                        self.execute_day2_operation(operation)
                    continue
                # Keep post-provisioning operations responsive even when no new
                # provisioning job is available.
                job = self.queue.claim_next_job(timeout_seconds=min(self.config.poll_timeout_seconds, 5))
                if job is None:
                    continue
                processed += 1
                with self._job_heartbeat_loop(job):
                    self.execute_job(job)
            except Exception as exc:
                self.logger.warning("Host runner loop recovered after queue/heartbeat error: %s", exc)
                time.sleep(3)

    def run_once(self) -> bool:
        self._write_idle_heartbeat()
        console = self.queue.claim_next_console_session(timeout_seconds=1)
        if console is not None:
            self._start_console_session(console)
            return True
        operation = self.queue.claim_next_day2_operation(timeout_seconds=1)
        if operation is not None:
            with self._day2_heartbeat_loop(operation):
                self.execute_day2_operation(operation)
            return True
        job = self.queue.claim_next_job(timeout_seconds=min(self.config.poll_timeout_seconds, 5))
        if job is None:
            return False
        with self._job_heartbeat_loop(job):
            self.execute_job(job)
        return True

    def _base_heartbeat_metadata(self, **extra: Any) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "pid": os.getpid(),
            "runner_code_fingerprint": _runner_code_fingerprint(),
            "runner_source": str(Path(__file__).resolve()),
            "template_name": self.config.template_name,
            "work_root": str(self.config.work_root),
        }
        metadata.update(extra)
        return metadata

    def _heartbeat_metadata(self, **extra: Any) -> dict[str, Any]:
        metadata = self._base_heartbeat_metadata(**extra)
        try:
            registered_vms = self.adapter.list_registered_vm_names()
            metadata["registered_vms"] = registered_vms
            inventory: list[dict[str, Any]] = []
            for vm_name in registered_vms:
                entry: dict[str, Any] = {"name": vm_name}
                try:
                    state = self.adapter.get_instance_state(vm_name)
                    entry.update(
                        {
                            "exists": bool(state.exists),
                            "power_state": state.power_state,
                            "provider_status": state.provider_status,
                        }
                    )
                except Exception as exc:
                    entry.update(
                        {
                            "exists": None,
                            "power_state": "unknown",
                            "provider_status": "unknown",
                            "error": str(exc),
                        }
                    )
                inventory.append(entry)
            metadata["registered_vm_inventory"] = inventory
        except Exception as exc:
            metadata["registered_vms_error"] = str(exc)
        return metadata

    def _write_idle_heartbeat(self) -> None:
        self.queue.write_runner_heartbeat(
            "idle",
            self._heartbeat_metadata(),
        )

    def _write_processing_heartbeat(self, job: ProvisioningJob, *, include_inventory: bool = True) -> None:
        metadata = (
            self._heartbeat_metadata(job_id=job.job_id, session_id=job.session_id)
            if include_inventory
            else self._base_heartbeat_metadata(job_id=job.job_id, session_id=job.session_id)
        )
        self.queue.write_runner_heartbeat(
            "processing",
            metadata,
        )

    def _write_day2_processing_heartbeat(
        self,
        operation: Day2OperationJob,
        *,
        include_inventory: bool = True,
    ) -> None:
        metadata_factory = self._heartbeat_metadata if include_inventory else self._base_heartbeat_metadata
        self.queue.write_runner_heartbeat(
            "server_management",
            metadata_factory(
                operation_id=operation.operation_id,
                operation=operation.operation,
                instance_id=operation.instance_id,
            ),
        )

    @contextmanager
    def _job_heartbeat_loop(self, job: ProvisioningJob, *, interval_seconds: float = 10.0):
        stop = threading.Event()

        def _pump() -> None:
            while not stop.wait(interval_seconds):
                try:
                    self._write_processing_heartbeat(job, include_inventory=False)
                except Exception as exc:
                    self.logger.debug("Processing heartbeat refresh failed for job %s: %s", job.job_id, exc)

        self._write_processing_heartbeat(job)
        thread = threading.Thread(
            target=_pump,
            name=f"ava-job-heartbeat-{job.job_id[:8]}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    @contextmanager
    def _day2_heartbeat_loop(self, operation: Day2OperationJob, *, interval_seconds: float = 10.0):
        stop = threading.Event()

        def _pump() -> None:
            while not stop.wait(interval_seconds):
                try:
                    self._write_day2_processing_heartbeat(operation, include_inventory=False)
                except Exception as exc:
                    self.logger.debug(
                        "Server-management heartbeat refresh failed for operation %s: %s",
                        operation.operation_id,
                        exc,
                    )

        self._write_day2_processing_heartbeat(operation)
        thread = threading.Thread(
            target=_pump,
            name=f"ava-day2-heartbeat-{operation.operation_id[:8]}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    def execute_day2_operation(self, operation: Day2OperationJob) -> None:
        self.logger.info(
            "Picked up server-management operation %s: %s on %s",
            operation.operation_id,
            operation.operation,
            operation.instance_id,
        )
        try:
            self.queue.write_day2_status(operation.operation_id, "running")
            if operation.operation == "snapshot":
                result = self._execute_snapshot(operation)
            elif operation.operation == "verify":
                result = self._execute_live_verify(operation)
            elif operation.operation == "nginx_logs":
                result = self._execute_nginx_logs(operation)
            elif operation.operation == "open_ssh_console":
                result = self._execute_open_ssh_console(operation)
            elif operation.operation == "restart_nginx":
                result = self._execute_restart_nginx(operation)
            elif operation.operation == "stop_vm":
                result = self._execute_stop_vm(operation)
            elif operation.operation == "start_vm":
                result = self._execute_start_vm(operation)
            elif operation.operation == "delete_vm":
                result = self._execute_delete_vm(operation)
            elif operation.operation == "check_updates":
                result = self._execute_check_updates(operation)
            elif operation.operation == "check_services":
                result = self._execute_check_services(operation)
            else:
                raise RuntimeError(
                    f"Operation '{operation.operation}' is approved but not executable yet. "
                    "Snapshot, live web verification, nginx log retrieval, SSH console launch, "
                    "nginx restart, VM start/stop, VM deletion, and guest inspection are enabled."
                )
            self.queue.write_day2_status(operation.operation_id, result.status)
            self.queue.write_day2_result(result)
            self.logger.info("Completed server-management operation %s", operation.operation_id)
        except Exception as exc:
            self.logger.exception(
                "Server-management operation %s failed: %s",
                operation.operation_id,
                exc,
            )
            self.queue.write_day2_status(operation.operation_id, "failed")
            self.queue.write_day2_result(
                Day2OperationResult(
                    operation_id=operation.operation_id,
                    operation=operation.operation,
                    status="failed",
                    instance_id=operation.instance_id,
                    instance_name=operation.instance_name,
                    evidence={"runner": "host_runner", "failed_at": _utc_now()},
                    error={"message": str(exc), "failure_class": "day2_operation_failed"},
                )
            )

    def _start_console_session(self, request: ConsoleSessionRequest) -> None:
        if request.console_id in self._console_threads and self._console_threads[request.console_id].is_alive():
            return
        worker = threading.Thread(
            target=self._run_console_session,
            args=(request,),
            name=f"ava-console-{request.console_id[:8]}",
            daemon=True,
        )
        self._console_threads[request.console_id] = worker
        worker.start()

    def _run_console_session(self, request: ConsoleSessionRequest) -> None:
        proc: subprocess.Popen[str] | None = None
        try:
            self._validate_binaries()
            state = self.adapter.get_instance_state(request.instance_id)
            if not state.exists:
                raise RuntimeError(f"VirtualBox VM '{request.instance_id}' does not exist")
            if state.power_state != "running":
                raise RuntimeError(f"VirtualBox VM '{request.instance_id}' is not running")

            key_path = self._find_runner_key_path_for(
                instance_id=request.instance_id,
                instance_name=request.instance_name,
                runner_job_id=request.runner_job_id,
            )
            if not key_path:
                raise RuntimeError(
                    "Runner SSH key is not available for this VM. Browser console requires a retained AVA runner key."
                )

            command = [
                self.config.ssh_binary,
                "-tt",
                "-i",
                str(key_path),
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "LogLevel=ERROR",
                "-o",
                "UserKnownHostsFile=" + _ssh_config_path_value(self._console_known_hosts_path(request)),
                "-p",
                str(request.ssh_port),
                f"{request.username}@{request.ssh_host}",
                self._console_shell_command(request),
            ]
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            self.queue.update_console_session(
                request.console_id,
                status="connected",
                pid=proc.pid,
                username=request.username,
                ssh_host=request.ssh_host,
                ssh_port=request.ssh_port,
                instance_id=request.instance_id,
                instance_name=request.instance_name,
            )
            self.queue.append_console_output(
                request.console_id,
                f"AVA Web Console connected to {request.instance_name or request.instance_id} as {request.username}.\r\n",
            )

            def _reader() -> None:
                assert proc is not None and proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(1)
                    if not chunk:
                        break
                    self.queue.append_console_output(request.console_id, chunk)

            reader = threading.Thread(target=_reader, name=f"ava-console-reader-{request.console_id[:8]}", daemon=True)
            reader.start()

            idle_deadline = time.monotonic() + 30 * 60
            while proc.poll() is None:
                state_doc = self.queue.get_console_session(request.console_id) or {}
                if state_doc.get("status") == "closing":
                    break
                item = self.queue.read_console_input(request.console_id, timeout_seconds=1)
                if item is not None:
                    idle_deadline = time.monotonic() + 30 * 60
                    if proc.stdin:
                        proc.stdin.write(item)
                        proc.stdin.flush()
                if time.monotonic() > idle_deadline:
                    self.queue.append_console_output(request.console_id, "\r\nAVA Web Console idle timeout reached.\r\n")
                    break

            if proc.poll() is None:
                proc.terminate()
            self.queue.update_console_session(request.console_id, status="closed", exit_code=proc.poll())
            self.queue.append_console_output(request.console_id, "\r\nAVA Web Console closed.\r\n")
        except Exception as exc:
            self.logger.exception("Console session %s failed: %s", request.console_id, exc)
            self.queue.update_console_session(request.console_id, status="failed", error=str(exc))
            self.queue.append_console_output(request.console_id, f"\r\nAVA Web Console failed: {exc}\r\n")
            if proc is not None and proc.poll() is None:
                proc.terminate()

    def _console_known_hosts_path(self, request: ConsoleSessionRequest) -> Path:
        """Use a scoped known_hosts file so recreated NAT VMs do not collide."""
        safe_console_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in request.console_id)
        path = self.config.work_root / "known_hosts_console_sessions" / f"{safe_console_id}.known_hosts"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _console_shell_command(self, request: ConsoleSessionRequest) -> str:
        """Start a quiet shell suitable for the browser terminal bridge."""
        label = request.instance_name or request.instance_id or "ava-vm"
        safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in label)
        prompt = f"{request.username}@{safe_label}:\\w$ "
        return (
            "TERM=xterm-256color "
            "PAGER=cat "
            "SYSTEMD_PAGER=cat "
            "SYSTEMD_COLORS=0 "
            "GIT_PAGER=cat "
            f"PS1='{prompt}' "
            "bash --noprofile --norc -i"
        )

    def _execute_snapshot(self, operation: Day2OperationJob) -> Day2OperationResult:
        self._validate_vboxmanage()
        state = self.adapter.get_instance_state(operation.instance_id)
        if not state.exists:
            raise RuntimeError(f"VirtualBox VM '{operation.instance_id}' does not exist")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        snapshot_name = f"ava-snapshot-{operation.operation_id[:8]}-{timestamp}"
        args = [
            "snapshot",
            operation.instance_id,
            "take",
            snapshot_name,
            "--description",
            f"AVA snapshot for approved operation {operation.operation_id}",
        ]
        if state.power_state == "running":
            args.append("--live")
        self.adapter._run_vboxmanage(*args, timeout=300)
        return Day2OperationResult(
            operation_id=operation.operation_id,
            operation=operation.operation,
            status="completed",
            instance_id=operation.instance_id,
            instance_name=operation.instance_name,
            evidence={
                "action": "snapshot_taken",
                "snapshot_name": snapshot_name,
                "power_state": state.power_state,
                "provider": "virtualbox",
                "runner": "host_runner",
            },
        )

    def _execute_open_ssh_console(self, operation: Day2OperationJob) -> Day2OperationResult:
        self._validate_vboxmanage()
        state = self.adapter.get_instance_state(operation.instance_id)
        if not state.exists:
            raise RuntimeError(f"VirtualBox VM '{operation.instance_id}' does not exist")
        if state.power_state != "running":
            raise RuntimeError(f"VirtualBox VM '{operation.instance_id}' is not running")

        connection = self.adapter.get_connection_info(operation.instance_id)
        host = operation.ssh_host or connection.host
        port = int(operation.ssh_port or connection.port)
        username = str((operation.metadata or {}).get("username") or "avaadmin").strip() or "avaadmin"

        putty = _find_putty_binary()
        if putty:
            tool = "PuTTY"
            command = _powershell_start_process_command(
                putty,
                ["-ssh", f"{username}@{host}", "-P", str(port)],
            )
        else:
            tool = "Windows OpenSSH"
            ssh_binary = str(self.config.ssh_binary or "ssh")
            if not Path(ssh_binary).exists():
                ssh_binary = shutil.which("ssh") or shutil.which("ssh.exe") or "ssh"
            ssh_command = f"& {_ps_single_quote(ssh_binary)} -p {port} {_ps_single_quote(f'{username}@{host}')}"
            command = _powershell_start_process_command(
                "powershell.exe",
                ["-NoExit", "-Command", ssh_command],
            )

        _run(command, timeout=30)
        return Day2OperationResult(
            operation_id=operation.operation_id,
            operation=operation.operation,
            status="completed",
            instance_id=operation.instance_id,
            instance_name=operation.instance_name,
            evidence={
                "action": "ssh_console_launched",
                "tool": tool,
                "ssh_host": host,
                "ssh_port": port,
                "username": username,
                "provider": "virtualbox",
                "runner": "host_runner",
                "password_handling": "not_passed_to_process",
            },
        )

    def _execute_live_verify(self, operation: Day2OperationJob) -> Day2OperationResult:
        self._validate_vboxmanage()
        checks: list[dict[str, object]] = []
        state = self.adapter.get_instance_state(operation.instance_id)
        checks.append(
            {
                "name": "vm_exists",
                "passed": state.exists,
                "evidence": f"provider_status={state.provider_status}",
            }
        )
        if state.exists:
            checks.append(
                {
                    "name": "vm_running",
                    "passed": state.power_state == "running",
                    "evidence": f"power_state={state.power_state}",
                }
            )

        connection = None
        if state.exists:
            connection = self.adapter.get_connection_info(operation.instance_id)
            checks.append(
                {
                    "name": "connection_info",
                    "passed": bool(connection.host and connection.port),
                    "evidence": f"{connection.host}:{connection.port}",
                }
            )
            checks.append(
                {
                    "name": "ssh_tcp_reachable",
                    "passed": _wait_for_tcp(connection.host, connection.port, 10),
                    "evidence": f"{connection.host}:{connection.port}",
                }
            )

        http_port = (
            operation.http_port
            or (connection.metadata.get("http_host_port") if connection is not None else None)
        )
        http_url = f"http://127.0.0.1:{http_port}/" if http_port else ""
        if http_url:
            ok, detail = _wait_for_http_200(http_url, timeout_seconds=15)
            checks.append(
                {
                    "name": "host_http_200",
                    "passed": ok,
                    "evidence": f"{http_url} -> {detail}",
                }
            )
        else:
            checks.append(
                {
                    "name": "host_http_200",
                    "passed": False,
                    "evidence": "missing http_host_port metadata",
                }
            )

        passed = all(bool(check.get("passed")) for check in checks)
        return Day2OperationResult(
            operation_id=operation.operation_id,
            operation=operation.operation,
            status="completed" if passed else "failed",
            instance_id=operation.instance_id,
            instance_name=operation.instance_name,
            evidence={
                "action": "live_web_verify",
                "checks": checks,
                "http_port": http_port,
                "ssh_host": operation.ssh_host or (connection.host if connection else None),
                "ssh_port": operation.ssh_port or (connection.port if connection else None),
                "provider": "virtualbox",
                "runner": "host_runner",
            },
            error=None if passed else {"message": "one or more live verification checks failed"},
        )

    def _execute_nginx_logs(self, operation: Day2OperationJob) -> Day2OperationResult:
        self._validate_binaries()
        key_path = self._find_runner_key_path(operation)
        if not key_path:
            return Day2OperationResult(
                operation_id=operation.operation_id,
                operation=operation.operation,
                status="failed",
                instance_id=operation.instance_id,
                instance_name=operation.instance_name,
                evidence={
                    "action": "nginx_logs",
                    "runner_key_available": False,
                    "runner": "host_runner",
                },
                error={
                    "message": (
                        "Runner SSH key is not available for this VM. Live guest log retrieval "
                        "requires a retained AVA runner key from provisioning."
                    ),
                    "failure_class": "ssh_auth_failed",
                },
            )

        connection = self.adapter.get_connection_info(operation.instance_id)
        executor = SSHExecutor(
            SSHConnection(
                host=connection.host,
                port=connection.port,
                username="ava-runner",
                private_key_path=str(key_path),
                known_hosts_path=str(self.config.work_root / "known_hosts_day2"),
                ssh_binary=self.config.ssh_binary,
            )
        )
        command = (
            "set -o pipefail; "
            "sudo journalctl -u nginx --no-pager -n 40 2>&1; "
            "printf '\\n--- AVA_ACCESS_LOG ---\\n'; "
            "sudo tail -n 30 /var/log/nginx/access.log 2>&1 || true; "
            "printf '\\n--- AVA_ERROR_LOG ---\\n'; "
            "sudo tail -n 30 /var/log/nginx/error.log 2>&1 || true"
        )
        log_result = executor.run(command, timeout_seconds=45)
        stdout = log_result.stdout or ""
        service_log, access_log, error_log = _split_nginx_log_output(stdout)
        status = "completed" if log_result.exit_code == 0 else "failed"
        return Day2OperationResult(
            operation_id=operation.operation_id,
            operation=operation.operation,
            status=status,
            instance_id=operation.instance_id,
            instance_name=operation.instance_name,
            evidence={
                "action": "nginx_logs",
                "journalctl_tail": service_log,
                "access_log_tail": access_log,
                "error_log_tail": error_log or log_result.stderr,
                "ssh_host": operation.ssh_host or connection.host,
                "ssh_port": operation.ssh_port or connection.port,
                "runner": "host_runner",
            },
            error=None if status == "completed" else {
                "message": (log_result.stderr or "nginx log command failed").strip()[:500],
                "failure_class": log_result.failure_class or "unknown",
            },
        )

    def _execute_check_updates(self, operation: Day2OperationJob) -> Day2OperationResult:
        self._validate_binaries()
        key_path = self._find_runner_key_path(operation)
        if not key_path:
            return Day2OperationResult(
                operation_id=operation.operation_id,
                operation=operation.operation,
                status="failed",
                instance_id=operation.instance_id,
                instance_name=operation.instance_name,
                evidence={"action": "check_updates", "runner_key_available": False, "runner": "host_runner"},
                error={
                    "message": (
                        "Runner SSH key is not available for this VM. Package inspection "
                        "requires a retained AVA runner key from provisioning."
                    ),
                    "failure_class": "ssh_auth_failed",
                },
            )
        connection = self.adapter.get_connection_info(operation.instance_id)
        executor = SSHExecutor(
            SSHConnection(
                host=connection.host,
                port=connection.port,
                username="ava-runner",
                private_key_path=str(key_path),
                known_hosts_path=str(self.config.work_root / "known_hosts_day2"),
                ssh_binary=self.config.ssh_binary,
            )
        )
        # Refresh the package index then list upgradable packages.
        # apt list exit code is 0 even when packages are listed; always non-mutating.
        command = (
            "sudo apt-get update -q 2>&1 | tail -3; "
            "echo '---AVA_UPGRADABLE---'; "
            "apt list --upgradable 2>/dev/null"
        )
        cmd_result = executor.run(command, timeout_seconds=120)
        stdout = cmd_result.stdout or ""
        upgradable_section = stdout.split("---AVA_UPGRADABLE---", 1)[-1] if "---AVA_UPGRADABLE---" in stdout else stdout
        total, security, packages, security_pkgs, high_impact, reboot_required = _parse_apt_upgradable(upgradable_section)
        status = "completed" if cmd_result.exit_code == 0 else "failed"
        return Day2OperationResult(
            operation_id=operation.operation_id,
            operation=operation.operation,
            status=status,
            instance_id=operation.instance_id,
            instance_name=operation.instance_name,
            evidence={
                "action": "check_updates",
                "total_upgradable": total,
                "security_updates": security,
                "packages": packages,
                "security_packages": security_pkgs,
                "high_impact": high_impact,
                "reboot_required": reboot_required,
                "raw_output": stdout[:3000],
                "ssh_host": operation.ssh_host or connection.host,
                "ssh_port": operation.ssh_port or connection.port,
                "runner": "host_runner",
            },
            error=None if status == "completed" else {
                "message": (cmd_result.stderr or "apt-get update failed").strip()[:500],
                "failure_class": cmd_result.failure_class or "package_manager_failed",
            },
        )

    def _execute_check_services(self, operation: Day2OperationJob) -> Day2OperationResult:
        self._validate_binaries()
        key_path = self._find_runner_key_path(operation)
        if not key_path:
            return Day2OperationResult(
                operation_id=operation.operation_id,
                operation=operation.operation,
                status="failed",
                instance_id=operation.instance_id,
                instance_name=operation.instance_name,
                evidence={"action": "check_services", "runner_key_available": False, "runner": "host_runner"},
                error={
                    "message": (
                        "Runner SSH key is not available for this VM. Service inspection "
                        "requires a retained AVA runner key from provisioning."
                    ),
                    "failure_class": "ssh_auth_failed",
                },
            )
        connection = self.adapter.get_connection_info(operation.instance_id)
        executor = SSHExecutor(
            SSHConnection(
                host=connection.host,
                port=connection.port,
                username="ava-runner",
                private_key_path=str(key_path),
                known_hosts_path=str(self.config.work_root / "known_hosts_day2"),
                ssh_binary=self.config.ssh_binary,
            )
        )
        command = (
            "echo '---AVA_RUNNING---'; "
            "systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null; "
            "echo '---AVA_FAILED---'; "
            "systemctl list-units --type=service --state=failed --no-pager --no-legend 2>/dev/null"
        )
        cmd_result = executor.run(command, timeout_seconds=30)
        stdout = cmd_result.stdout or ""
        running_section = ""
        failed_section = ""
        if "---AVA_RUNNING---" in stdout and "---AVA_FAILED---" in stdout:
            parts = stdout.split("---AVA_RUNNING---", 1)[1].split("---AVA_FAILED---", 1)
            running_section = parts[0]
            failed_section = parts[1] if len(parts) > 1 else ""
        running = _parse_systemd_units(running_section)
        failed = _parse_systemd_units(failed_section)
        status = "completed" if cmd_result.exit_code == 0 else "failed"
        return Day2OperationResult(
            operation_id=operation.operation_id,
            operation=operation.operation,
            status=status,
            instance_id=operation.instance_id,
            instance_name=operation.instance_name,
            evidence={
                "action": "check_services",
                "running": running,
                "failed": failed,
                "running_count": len(running),
                "failed_count": len(failed),
                "raw_output": stdout[:3000],
                "ssh_host": operation.ssh_host or connection.host,
                "ssh_port": operation.ssh_port or connection.port,
                "runner": "host_runner",
            },
            error=None if status == "completed" else {
                "message": (cmd_result.stderr or "systemctl command failed").strip()[:500],
                "failure_class": cmd_result.failure_class or "service_failed",
            },
        )

    def _execute_restart_nginx(self, operation: Day2OperationJob) -> Day2OperationResult:
        self._validate_binaries()
        key_path = self._find_runner_key_path(operation)
        if not key_path:
            return Day2OperationResult(
                operation_id=operation.operation_id,
                operation=operation.operation,
                status="failed",
                instance_id=operation.instance_id,
                instance_name=operation.instance_name,
                evidence={
                    "action": "service_restart",
                    "service": "nginx",
                    "runner_key_available": False,
                    "runner": "host_runner",
                },
                error={
                    "message": (
                        "Runner SSH key is not available for this VM. Restarting nginx "
                        "requires the retained AVA runner key from provisioning."
                    ),
                    "failure_class": "ssh_auth_failed",
                },
            )

        connection = self.adapter.get_connection_info(operation.instance_id)
        executor = SSHExecutor(
            SSHConnection(
                host=connection.host,
                port=connection.port,
                username="ava-runner",
                private_key_path=str(key_path),
                known_hosts_path=str(self.config.work_root / "known_hosts_day2"),
                ssh_binary=self.config.ssh_binary,
            )
        )
        command = (
            "sudo systemctl restart nginx && "
            "systemctl is-active nginx && "
            "curl -fsS -o /dev/null http://127.0.0.1"
        )
        restart_result = executor.run(command, timeout_seconds=60)
        status = "completed" if restart_result.exit_code == 0 else "failed"
        return Day2OperationResult(
            operation_id=operation.operation_id,
            operation=operation.operation,
            status=status,
            instance_id=operation.instance_id,
            instance_name=operation.instance_name,
            evidence={
                "action": "service_restarted",
                "service": "nginx",
                "stdout": (restart_result.stdout or "").strip()[:1000],
                "stderr": (restart_result.stderr or "").strip()[:1000],
                "ssh_host": operation.ssh_host or connection.host,
                "ssh_port": operation.ssh_port or connection.port,
                "runner": "host_runner",
            },
            error=None if status == "completed" else {
                "message": (restart_result.stderr or restart_result.stdout or "nginx restart failed").strip()[:500],
                "failure_class": restart_result.failure_class or "service_restart_failed",
            },
        )

    def _execute_stop_vm(self, operation: Day2OperationJob) -> Day2OperationResult:
        self._validate_vboxmanage()
        before = self.adapter.get_instance_state(operation.instance_id)
        if not before.exists:
            return self._vm_lifecycle_result(
                operation,
                status="completed",
                action="vm_already_absent",
                before=before,
                after=before,
                message="VM was already absent.",
            )
        if before.power_state != "running":
            return self._vm_lifecycle_result(
                operation,
                status="completed",
                action="vm_already_stopped",
                before=before,
                after=before,
                message="VM was already stopped.",
            )

        self.adapter.stop_instance(operation.instance_id)
        after = self._wait_for_vm_state(operation.instance_id, lambda state: not state.exists or state.power_state != "running")
        completed = not after.exists or after.power_state != "running"
        return self._vm_lifecycle_result(
            operation,
            status="completed" if completed else "failed",
            action="vm_stopped",
            before=before,
            after=after,
            message="VM stopped." if completed else "VM did not stop before timeout.",
        )

    def _execute_start_vm(self, operation: Day2OperationJob) -> Day2OperationResult:
        self._validate_vboxmanage()
        before = self.adapter.get_instance_state(operation.instance_id)
        if not before.exists:
            return self._vm_lifecycle_result(
                operation,
                status="failed",
                action="vm_missing",
                before=before,
                after=before,
                message=f"VirtualBox VM '{operation.instance_id}' does not exist.",
                failure_class="vm_not_found",
            )
        if before.power_state == "running":
            return self._vm_lifecycle_result(
                operation,
                status="completed",
                action="vm_already_running",
                before=before,
                after=before,
                message="VM was already running.",
            )

        self.adapter.start_instance(operation.instance_id)
        after = self._wait_for_vm_state(operation.instance_id, lambda state: state.exists and state.power_state == "running")
        completed = after.exists and after.power_state == "running"
        return self._vm_lifecycle_result(
            operation,
            status="completed" if completed else "failed",
            action="vm_started",
            before=before,
            after=after,
            message="VM started." if completed else "VM did not reach running state before timeout.",
        )

    def _execute_delete_vm(self, operation: Day2OperationJob) -> Day2OperationResult:
        self._validate_vboxmanage()
        before = self.adapter.get_instance_state(operation.instance_id)
        if not before.exists:
            return self._vm_lifecycle_result(
                operation,
                status="completed",
                action="vm_already_deleted",
                before=before,
                after=before,
                message="VM was already absent.",
            )

        self.adapter.destroy_instance(operation.instance_id)
        after = self._wait_for_vm_state(operation.instance_id, lambda state: not state.exists)
        completed = not after.exists
        return self._vm_lifecycle_result(
            operation,
            status="completed" if completed else "failed",
            action="vm_deleted",
            before=before,
            after=after,
            message="VM deleted." if completed else "VM still exists after delete request.",
            failure_class=None if completed else "vm_delete_failed",
        )

    def _wait_for_vm_state(self, instance_id: str, predicate, *, timeout_seconds: int = 60):
        deadline = time.monotonic() + timeout_seconds
        state = self.adapter.get_instance_state(instance_id)
        while time.monotonic() < deadline:
            if predicate(state):
                return state
            time.sleep(2)
            state = self.adapter.get_instance_state(instance_id)
        return state

    def _vm_lifecycle_result(
        self,
        operation: Day2OperationJob,
        *,
        status: str,
        action: str,
        before,
        after,
        message: str,
        failure_class: str | None = None,
    ) -> Day2OperationResult:
        evidence = {
            "action": action,
            "message": message,
            "provider": "virtualbox",
            "runner": "host_runner",
            "before": _provider_state_dict(before),
            "after": _provider_state_dict(after),
        }
        return Day2OperationResult(
            operation_id=operation.operation_id,
            operation=operation.operation,
            status=status,
            instance_id=operation.instance_id,
            instance_name=operation.instance_name,
            evidence=evidence,
            error=None if status == "completed" else {"message": message, "failure_class": failure_class or action},
        )

    def _find_runner_key_path(self, operation: Day2OperationJob) -> Path | None:
        return self._find_runner_key_path_for(
            instance_id=operation.instance_id,
            instance_name=operation.instance_name,
            runner_job_id=str((operation.metadata or {}).get("runner_job_id") or "").strip() or None,
            metadata_key=str((operation.metadata or {}).get("runner_key_path") or "").strip() or None,
        )

    def _find_runner_key_path_for(
        self,
        *,
        instance_id: str,
        instance_name: str | None = None,
        runner_job_id: str | None = None,
        metadata_key: str | None = None,
    ) -> Path | None:
        candidates: list[Path] = []
        if metadata_key:
            candidates.append(Path(metadata_key))
        if runner_job_id:
            candidates.append(self.config.work_root / "keys" / f"{runner_job_id}_ava_runner_ed25519")
            candidates.append(self.config.work_root / runner_job_id / "ava_runner_ed25519")
        if instance_name:
            candidates.append(self.config.work_root / "keys" / f"{instance_name}_ava_runner_ed25519")
        candidates.append(self.config.work_root / "keys" / f"{instance_id}_ava_runner_ed25519")
        for candidate in candidates:
            try:
                if candidate.exists():
                    _lock_down_private_key(candidate)
                    return candidate
            except OSError:
                self.logger.warning("Skipping inaccessible retained runner key candidate: %s", candidate)
        return None

    def execute_job(self, job: ProvisioningJob) -> None:
        instance_id: str | None = None
        connection = None
        retained_key_path: Path | None = None
        progress_instance_name: str | None = None
        work_dir = self.config.work_root / job.job_id
        secret_patterns = (str(job.credentials_seed_data.get("temporary_password") or ""),)
        self.logger.info("Picked up job %s for session %s", job.job_id, job.session_id)
        heartbeat = lambda: self._write_processing_heartbeat(job)

        def write_progress(status: str, stage: str, message: str) -> None:
            http_port = None
            if connection is not None:
                http_port = connection.metadata.get("http_host_port")
            self.writer.progress(
                job_id=job.job_id,
                session_id=job.session_id,
                status=status,
                stage=stage,
                instance_id=instance_id,
                instance_name=progress_instance_name or (job.desired_state or {}).get("vm_name"),
                ssh_host=connection.host if connection is not None else None,
                ssh_port=connection.port if connection is not None else None,
                http_port=int(http_port) if http_port else None,
                runner_key_path=str(retained_key_path) if retained_key_path else None,
                message=message,
            )

        try:
            self._validate_binaries()
            work_dir.mkdir(parents=True, exist_ok=True)
            desired_state = dict(job.desired_state)
            username = str(job.credentials_seed_data.get("username") or desired_state.get("username") or "avaadmin")
            temporary_password = str(job.credentials_seed_data.get("temporary_password") or "")
            if not temporary_password:
                raise RuntimeError("Job is missing temporary provisioning password")
            desired_state["username"] = username

            vm_name = str(desired_state.get("vm_name") or "").strip()
            if not vm_name:
                vm_name = f"ava-web-{job.job_id[:8]}"
                desired_state["vm_name"] = vm_name
            progress_instance_name = vm_name

            write_progress("provisioning", "preparing", "Preparing SSH key, cloud-init seed, and VirtualBox plan.")
            key_path = work_dir / "ava_runner_ed25519"
            known_hosts_path = work_dir / "known_hosts"
            seed_dir = work_dir / "seed"
            seed_iso = work_dir / "seed.iso"
            _run([self.config.ssh_keygen, "-t", "ed25519", "-N", "", "-f", str(key_path), "-C", f"ava-runner-{vm_name}"], timeout=30)
            _lock_down_private_key(key_path)
            public_key = (key_path.with_suffix(".pub")).read_text(encoding="utf-8").strip()
            retained_key_dir = self.config.work_root / "keys"
            retained_key_dir.mkdir(parents=True, exist_ok=True)
            retained_key_path = retained_key_dir / f"{job.job_id}_ava_runner_ed25519"
            shutil.copy2(key_path, retained_key_path)
            shutil.copy2(key_path.with_suffix(".pub"), retained_key_path.with_suffix(".pub"))
            _lock_down_private_key(retained_key_path)
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

            plan = self.adapter.plan_instance(desired_state)
            instance_id = self.adapter.create_instance(plan)
            write_progress("provisioning", "vm_created", "VirtualBox VM has been created; access seed is being attached.")
            access_result = self.adapter.inject_access(
                instance_id,
                {
                    "seed_iso_path": str(seed_iso),
                    "username": username,
                    "temporary_password": temporary_password,
                },
            )
            if access_result != "cloud_init_seed_attached":
                raise RuntimeError(f"Unexpected access injection result: {access_result}")

            connection = self.adapter.get_connection_info(instance_id)
            self.adapter.start_instance(instance_id)
            write_progress("provisioning", "vm_started", "VM has started; waiting for SSH TCP readiness.")
            if not _wait_for_tcp(connection.host, connection.port, self.config.timeout_seconds, heartbeat=heartbeat):
                raise RuntimeError(f"SSH TCP did not become reachable at {connection.host}:{connection.port}")
            write_progress("provisioning", "ssh_tcp_ready", "SSH TCP is reachable; waiting for SSH login readiness.")

            # ava-runner holds the key; avaadmin has chage -d 0 (expired by design)
            # which blocks PAM account validation for all SSH sessions, key auth included.
            executor = SSHExecutor(
                SSHConnection(
                    host=connection.host,
                    port=connection.port,
                    username="ava-runner",
                    private_key_path=str(key_path),
                    known_hosts_path=str(known_hosts_path),
                    ssh_binary=self.config.ssh_binary,
                )
            )
            ssh_ready = _wait_for_executor_command(
                executor,
                "printf 'AVA_SSH_READY'",
                self.config.timeout_seconds,
                redact=secret_patterns,
                heartbeat=heartbeat,
                success_stdout_contains="AVA_SSH_READY",
                attempt_timeout_seconds=15,
            )
            if not ssh_ready or "AVA_SSH_READY" not in (ssh_ready.stdout or ""):
                if ssh_ready:
                    detail = (
                        f"SSH login was not ready after TCP became reachable "
                        f"(exit_code={ssh_ready.exit_code}, failure_class={ssh_ready.failure_class}, "
                        f"stdout={ssh_ready.stdout[-300:]!r}, stderr={ssh_ready.stderr[-300:]!r})"
                    )
                else:
                    detail = "SSH login was not ready after TCP became reachable (no SSH command result)"
                raise RuntimeError(detail)
            write_progress("provisioning", "ssh_ready", "SSH login is ready; waiting for cloud-init first-access marker.")

            cloud_init = _wait_for_executor_command(
                executor,
                "test -f /var/tmp/ava-cloud-init-ready && cat /var/tmp/ava-cloud-init-ready",
                self.config.timeout_seconds,
                redact=secret_patterns,
                heartbeat=heartbeat,
                success_stdout_contains=f"AVA_CLOUD_INIT_READY {vm_name}",
                attempt_timeout_seconds=15,
            )
            if not cloud_init or f"AVA_CLOUD_INIT_READY {vm_name}" not in cloud_init.stdout:
                if cloud_init:
                    detail = (
                        f"cloud-init first-access marker was not confirmed "
                        f"(exit_code={cloud_init.exit_code}, failure_class={cloud_init.failure_class}, "
                        f"stdout={cloud_init.stdout[-300:]!r}, stderr={cloud_init.stderr[-300:]!r})"
                    )
                else:
                    detail = "cloud-init first-access marker was not confirmed (no SSH command result)"
                raise RuntimeError(detail)
            write_progress("provisioning", "cloud_init_ready", "Cloud-init first-access marker confirmed.")

            self._detach_seed_iso(instance_id, seed_iso)
            # seed.iso is detached from the VM; cleanup is deferred to after
            # verification so that a VBoxSVC file-lock race does not destroy
            # a fully working VM.

            write_progress("bootstrapping", "web_bootstrap", "Installing and enabling the web-server role.")
            role = WebServerRole()
            bootstrap_heartbeat = lambda: self._write_processing_heartbeat(job, include_inventory=False)
            try:
                bootstrap_params = inspect.signature(role.bootstrap).parameters
            except (TypeError, ValueError):
                bootstrap_params = {}
            if "heartbeat" in bootstrap_params:
                results = role.bootstrap(executor, heartbeat=bootstrap_heartbeat)
            else:
                results = role.bootstrap(executor)
            for result in results:
                if result.exit_code != 0:
                    failure_class = result.failure_class or "bootstrap_failed"
                    detail = result.stderr or result.stdout or f"bootstrap command failed: {result.command}"
                    message = (
                        f"{failure_class}: {detail} "
                        f"(exit_code={result.exit_code}, command={result.command})"
                    )
                    if _is_recoverable_guest_failure(failure_class):
                        write_progress(
                            "provisioning",
                            failure_class,
                            "The VM exists, but Ubuntu guest readiness did not complete before the bootstrap timeout.",
                        )
                        raise RecoverableGuestReadinessError(message)
                    raise RuntimeError(message)

            if desired_state.get("hardening_profile") != "none":
                write_progress("hardening", "baseline_hardening", "Baseline Linux and web role hardening is being applied.")

            write_progress("verifying", "http_verification", "Verifying host HTTP and guest web-server health.")
            http_port = connection.metadata.get("http_host_port")
            if not http_port:
                raise RuntimeError("HTTP host port was not configured")
            ok, detail = _wait_for_http_200(
                f"http://127.0.0.1:{http_port}/",
                timeout_seconds=120,
                heartbeat=heartbeat,
            )
            if not ok:
                raise RuntimeError(f"HTTP verification failed: {detail}")

            report = VerificationEngine(
                self.adapter,
                executor_factory=lambda _connection: executor,
            ).verify_web_server(instance_id)
            if not report.passed:
                raise RuntimeError("verification engine reported failure")

            # VM is fully verified. Attempt secret file cleanup now that bootstrap
            # and verification are complete. VBoxSVC may still hold seed.iso after
            # closemedium returns — this is a known Windows file-lock race. A failure
            # here must NOT destroy a working VM; log and continue.
            cleanup_warning: str | None = None
            try:
                self._cleanup_secret_files(work_dir, seed_dir, seed_iso)
            except Exception as cleanup_exc:
                cleanup_warning = str(cleanup_exc)
                self.logger.warning(
                    "Job %s: VM provisioned successfully but seed.iso cleanup failed — "
                    "manual cleanup required at %s. Job marked completed.",
                    job.job_id, work_dir,
                )

            evidence = report.to_dict()
            if cleanup_warning:
                evidence["cleanup_warning"] = cleanup_warning

            self.writer.completed(
                job_id=job.job_id,
                session_id=job.session_id,
                instance_id=instance_id,
                instance_name=vm_name,
                ssh_host=connection.host,
                ssh_port=connection.port,
                http_port=int(http_port),
                verification_evidence=evidence,
            )
            self.logger.info("Completed job %s instance=%s", job.job_id, instance_id)
        except Exception as exc:
            self.logger.exception("Job %s failed: %s", job.job_id, _redact(str(exc), secret_patterns))
            failure_class = _runner_failure_class(exc)
            retain_partial_vm = self.config.retain_debug or _is_recoverable_guest_failure(failure_class)
            rollback_report = ProvisioningRollbackManager(self.adapter).handle_failure(
                session_id=job.session_id,
                phase=self.queue.get_status(job.job_id) or "unknown",
                failed_step="host_runner",
                failure_class=failure_class,
                message=_redact(str(exc), secret_patterns),
                instance_id=instance_id,
                retain_for_debug=retain_partial_vm,
            )
            self.writer.failed(
                job_id=job.job_id,
                session_id=job.session_id,
                instance_id=instance_id,
                instance_name=(job.desired_state or {}).get("vm_name"),
                error=rollback_report.to_dict(),
                verification_evidence={"rollback": rollback_report.to_dict()},
            )
        finally:
            if not self.config.retain_debug:
                shutil.rmtree(work_dir, ignore_errors=True)

    def _validate_binaries(self) -> None:
        for label, path in (
            ("VBoxManage", self.config.vboxmanage),
            ("ssh", self.config.ssh_binary),
            ("ssh-keygen", self.config.ssh_keygen),
        ):
            if not Path(path).exists():
                raise RuntimeError(f"{label} not found at {path}")

    def _validate_vboxmanage(self) -> None:
        if not Path(self.config.vboxmanage).exists():
            raise RuntimeError(f"VBoxManage not found at {self.config.vboxmanage}")

    def _detach_seed_iso(self, instance_id: str, seed_iso: Path) -> None:
        self.adapter._run_vboxmanage(
            "storageattach",
            instance_id,
            "--storagectl",
            "AVA-Seed",
            "--port",
            "0",
            "--device",
            "0",
            "--medium",
            "none",
            "--forceunmount",
            check=False,
        )
        # storageattach --medium none disconnects the ISO from the storage
        # controller but VBoxSVC on Windows keeps the file handle open until
        # the medium is removed from VirtualBox's global registry.
        # closemedium dvd releases that handle so seed_iso.unlink() can succeed.
        close_proc = self.adapter._run_vboxmanage(
            "closemedium",
            "dvd",
            str(seed_iso),
            check=False,
        )
        if close_proc.returncode != 0:
            seed_uuid = self._find_registered_seed_dvd_uuid(seed_iso)
            if seed_uuid:
                self.adapter._run_vboxmanage("closemedium", "dvd", seed_uuid, check=False)

    def _find_registered_seed_dvd_uuid(self, seed_iso: Path) -> str | None:
        proc = self.adapter._run_vboxmanage("list", "dvds", check=False)
        if proc.returncode != 0:
            return None
        target = str(seed_iso).lower()
        current_uuid: str | None = None
        for raw_line in (proc.stdout or "").splitlines():
            line = raw_line.strip()
            if line.startswith("UUID:"):
                current_uuid = line.split(":", 1)[1].strip()
            elif line.startswith("Location:") and line.split(":", 1)[1].strip().lower() == target:
                return current_uuid
        return None

    def _cleanup_secret_files(self, work_dir: Path, seed_dir: Path, seed_iso: Path) -> None:
        # VBoxSVC releases its file handle on seed.iso asynchronously after
        # closemedium returns (WinError 32 race). Retry on PermissionError only;
        # any other exception propagates immediately.
        last_exc: Exception | None = None
        for attempt in range(1, _ISO_UNLINK_ATTEMPTS + 1):
            if not seed_iso.exists():
                break
            try:
                seed_iso.unlink()
                break
            except PermissionError as exc:
                last_exc = exc
                self.logger.warning(
                    "seed.iso locked by another process (attempt %d/%d), retrying in %.0fs",
                    attempt, _ISO_UNLINK_ATTEMPTS, _ISO_UNLINK_DELAY_SECONDS,
                )
                time.sleep(_ISO_UNLINK_DELAY_SECONDS)
        else:
            raise RuntimeError(f"Secret cleanup failed in {work_dir}: {last_exc}") from last_exc
        try:
            if seed_dir.exists():
                shutil.rmtree(seed_dir)
        except Exception as exc:
            raise RuntimeError(f"Secret cleanup failed in {work_dir}: {exc}") from exc


def _build_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ava.provisioning.host_runner")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    return logger


def _redact(text: str, patterns: tuple[str, ...]) -> str:
    redacted = text or ""
    for pattern in patterns:
        if pattern:
            redacted = redacted.replace(pattern, "[REDACTED]")
    return redacted


_HIGH_IMPACT_PREFIXES: tuple[str, ...] = (
    "linux-image", "linux-headers", "linux-virtual",
    "libssl", "openssl", "openssh", "sudo", "bind9",
    "libc", "glibc", "ca-certificates", "curl",
)
_REBOOT_PREFIXES: tuple[str, ...] = ("linux-image", "linux-headers", "linux-virtual")


def _parse_apt_upgradable(
    stdout: str,
) -> tuple[int, int, list[str], list[str], list[str], bool]:
    """Parse `apt list --upgradable 2>/dev/null` output.

    Returns (total_upgradable, security_count, packages, security_packages, high_impact, reboot_required).
    Skips the "Listing..." header line emitted by apt.
    """
    packages: list[str] = []
    security_packages: list[str] = []
    high_impact: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Listing...") or line.startswith("WARNING"):
            continue
        # Format: "package/source version arch [upgradable from: old]"
        name = line.split("/")[0].strip()
        if not name:
            continue
        packages.append(name)
        if "-security" in line:
            security_packages.append(name)
        if any(name.startswith(p) for p in _HIGH_IMPACT_PREFIXES):
            high_impact.append(name)
    reboot_required = any(name.startswith(p) for name in packages for p in _REBOOT_PREFIXES)
    return len(packages), len(security_packages), packages, security_packages, high_impact, reboot_required


def _parse_systemd_units(stdout: str) -> list[str]:
    """Parse `systemctl list-units --no-legend` output into service names."""
    names: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: "  unit.service  loaded active running  Description"
        # Leading bullet (●) or space, then unit name as first token
        parts = line.lstrip("● ").split()
        if parts:
            unit = parts[0]
            if unit.endswith(".service"):
                names.append(unit[: -len(".service")])
            elif "." not in unit:
                names.append(unit)
    return names


def _runner_failure_class(exc: Exception) -> str:
    message = str(exc or "").lower()
    if "virtualbox vm" in message and "already exists" in message:
        return "vm_name_conflict"
    if "dns resolution not ready" in message or "ubuntu package mirrors" in message:
        return "guest_network_not_ready"
    if (
        "guest_readiness_timeout" in message
        or "guest readiness was not confirmed" in message
        or "ssh login was not ready" in message
        or "cloud-init first-access marker" in message
        or "sudo -n true" in message
        or "systemctl is-active ssh" in message
        or "connection timed out during banner exchange" in message
    ):
        return "guest_readiness_timeout"
    return "runner_failed"


def main() -> int:
    try:
        HostRunner().run_forever()
        return 0
    except KeyboardInterrupt:
        print("AVA host runner stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
