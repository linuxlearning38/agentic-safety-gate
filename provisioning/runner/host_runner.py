"""Windows host-side runner for AVA v2 chat-approved provisioning jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any, Callable
from urllib.request import urlopen

from provisioning.adapters.virtualbox import VirtualBoxAdapter
from provisioning.bootstrap import SSHConnection, SSHExecutor
from provisioning.rollback import ProvisioningRollbackManager
from provisioning.roles.web_server import WebServerRole
from provisioning.verify import VerificationEngine

from .job_queue import Day2OperationJob, Day2OperationResult, ProvisioningJob, RedisProvisioningJobQueue
from .result_writer import ProvisioningResultWriter


ROOT = Path(__file__).resolve().parents[2]


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
):
    deadline = time.monotonic() + timeout_seconds
    last_result = None
    while time.monotonic() < deadline:
        if heartbeat:
            heartbeat()
        last_result = executor.run(command, timeout_seconds=30, redact_patterns=redact)
        if last_result.exit_code == 0:
            return last_result
        time.sleep(8)
    return last_result


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

    def run_forever(self) -> None:
        processed = 0
        self.logger.info("AVA host runner started")
        while True:
            self._write_idle_heartbeat()
            if self.config.max_jobs is not None and processed >= self.config.max_jobs:
                self.logger.info("Max jobs reached; exiting")
                return
            operation = self.queue.claim_next_day2_operation(timeout_seconds=1)
            if operation is not None:
                self._write_day2_processing_heartbeat(operation)
                processed += 1
                self.execute_day2_operation(operation)
                continue
            # Keep post-provisioning operations responsive even when no new
            # provisioning job is available.
            job = self.queue.claim_next_job(timeout_seconds=min(self.config.poll_timeout_seconds, 5))
            if job is None:
                continue
            self._write_processing_heartbeat(job)
            processed += 1
            self.execute_job(job)

    def run_once(self) -> bool:
        self._write_idle_heartbeat()
        operation = self.queue.claim_next_day2_operation(timeout_seconds=1)
        if operation is not None:
            self._write_day2_processing_heartbeat(operation)
            self.execute_day2_operation(operation)
            return True
        job = self.queue.claim_next_job(timeout_seconds=min(self.config.poll_timeout_seconds, 5))
        if job is None:
            return False
        self._write_processing_heartbeat(job)
        self.execute_job(job)
        return True

    def _write_idle_heartbeat(self) -> None:
        self.queue.write_runner_heartbeat(
            "idle",
            {
                "pid": os.getpid(),
                "template_name": self.config.template_name,
                "work_root": str(self.config.work_root),
            },
        )

    def _write_processing_heartbeat(self, job: ProvisioningJob) -> None:
        self.queue.write_runner_heartbeat(
            "processing",
            {
                "pid": os.getpid(),
                "job_id": job.job_id,
                "session_id": job.session_id,
            },
        )

    def _write_day2_processing_heartbeat(self, operation: Day2OperationJob) -> None:
        self.queue.write_runner_heartbeat(
            "server_management",
            {
                "pid": os.getpid(),
                "operation_id": operation.operation_id,
                "operation": operation.operation,
                "instance_id": operation.instance_id,
            },
        )

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
            else:
                raise RuntimeError(
                    f"Operation '{operation.operation}' is approved but not executable yet. "
                    "Snapshot execution is enabled first; guest SSH actions require durable runner identity."
                )
            self.queue.write_day2_status(operation.operation_id, "completed")
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

    def execute_job(self, job: ProvisioningJob) -> None:
        instance_id: str | None = None
        work_dir = self.config.work_root / job.job_id
        secret_patterns = (str(job.credentials_seed_data.get("temporary_password") or ""),)
        self.logger.info("Picked up job %s for session %s", job.job_id, job.session_id)
        heartbeat = lambda: self._write_processing_heartbeat(job)
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

            self.writer.status(job.job_id, "provisioning")
            key_path = work_dir / "ava_runner_ed25519"
            known_hosts_path = work_dir / "known_hosts"
            seed_dir = work_dir / "seed"
            seed_iso = work_dir / "seed.iso"
            _run([self.config.ssh_keygen, "-t", "ed25519", "-N", "", "-f", str(key_path), "-C", f"ava-runner-{vm_name}"], timeout=30)
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

            plan = self.adapter.plan_instance(desired_state)
            instance_id = self.adapter.create_instance(plan)
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
            if not _wait_for_tcp(connection.host, connection.port, self.config.timeout_seconds, heartbeat=heartbeat):
                raise RuntimeError(f"SSH TCP did not become reachable at {connection.host}:{connection.port}")

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
            cloud_init = _wait_for_executor_command(
                executor,
                "cloud-init status --wait >/tmp/ava-cloud-init-status.txt 2>&1; test -f /var/tmp/ava-cloud-init-ready; cat /var/tmp/ava-cloud-init-ready",
                self.config.timeout_seconds,
                redact=secret_patterns,
                heartbeat=heartbeat,
            )
            if not cloud_init or cloud_init.exit_code != 0 or f"AVA_CLOUD_INIT_READY {vm_name}" not in cloud_init.stdout:
                if cloud_init:
                    detail = (
                        f"cloud-init first-access marker was not confirmed "
                        f"(exit_code={cloud_init.exit_code}, failure_class={cloud_init.failure_class}, "
                        f"stdout={cloud_init.stdout[-300:]!r}, stderr={cloud_init.stderr[-300:]!r})"
                    )
                else:
                    detail = "cloud-init first-access marker was not confirmed (no SSH command result)"
                raise RuntimeError(detail)

            self._detach_seed_iso(instance_id, seed_iso)
            # seed.iso is detached from the VM; cleanup is deferred to after
            # verification so that a VBoxSVC file-lock race does not destroy
            # a fully working VM.

            self.writer.status(job.job_id, "bootstrapping")
            role = WebServerRole()
            results = role.bootstrap(executor)
            for result in results:
                if result.exit_code != 0:
                    raise RuntimeError(result.stderr or f"bootstrap command failed: {result.command}")

            if desired_state.get("hardening_profile") != "none":
                self.writer.status(job.job_id, "hardening")

            self.writer.status(job.job_id, "verifying")
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
            rollback_report = ProvisioningRollbackManager(self.adapter).handle_failure(
                session_id=job.session_id,
                phase=self.queue.get_status(job.job_id) or "unknown",
                failed_step="host_runner",
                failure_class="runner_failed",
                message=_redact(str(exc), secret_patterns),
                instance_id=instance_id,
                retain_for_debug=self.config.retain_debug,
            )
            self.writer.failed(
                job_id=job.job_id,
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


def main() -> int:
    try:
        HostRunner().run_forever()
        return 0
    except KeyboardInterrupt:
        print("AVA host runner stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
