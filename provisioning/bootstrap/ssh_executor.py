"""Structured SSH command executor for AVA v2 bootstrap work."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import time
from typing import Iterable


FAILURE_CLASSES = {
    "ssh_connect_timeout",
    "ssh_auth_failed",
    "command_timeout",
    "package_manager_failed",
    "service_failed",
    "firewall_failed",
    "verification_failed",
    "unknown",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_failure(command: str, exit_code: int | None, stderr: str, *, timed_out: bool = False) -> str | None:
    """Classify command failures into the fixed Phase 4 vocabulary."""

    if exit_code == 0 and not timed_out:
        return None
    lowered_command = (command or "").lower()
    lowered_error = (stderr or "").lower()
    if timed_out:
        return "command_timeout"
    if "permission denied" in lowered_error or "publickey" in lowered_error or "authentication" in lowered_error:
        return "ssh_auth_failed"
    if "connection timed out" in lowered_error or "connection refused" in lowered_error or "could not resolve" in lowered_error:
        return "ssh_connect_timeout"
    if "apt-get" in lowered_command or "dpkg" in lowered_command:
        return "package_manager_failed"
    if "systemctl" in lowered_command:
        return "service_failed"
    if "ufw" in lowered_command:
        return "firewall_failed"
    if "curl" in lowered_command or "test " in lowered_command or "is-active" in lowered_command:
        return "verification_failed"
    return "unknown"


def _redact(text: str, redact_patterns: Iterable[str]) -> str:
    redacted = text or ""
    for pattern in redact_patterns:
        if pattern:
            redacted = redacted.replace(pattern, "[REDACTED]")
    return redacted


@dataclass(slots=True)
class SSHConnection:
    """SSH connection target for a provisioned guest."""

    host: str
    port: int
    username: str
    private_key_path: str | None = None
    temporary_password: str | None = None
    known_hosts_path: str | None = None
    ssh_binary: str = "ssh"


@dataclass(slots=True)
class SSHCommandResult:
    """Structured evidence for one SSH command."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    started_at: str
    finished_at: str
    timed_out: bool
    failure_class: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SSHExecutor:
    """Run commands over OpenSSH while preserving structured evidence."""

    def __init__(self, connection: SSHConnection):
        self.connection = connection

    def run(self, command: str, *, timeout_seconds: int = 120, redact_patterns: Iterable[str] = ()) -> SSHCommandResult:
        started_at = _utc_now()
        start = time.monotonic()
        if not self.connection.private_key_path:
            finished_at = _utc_now()
            return SSHCommandResult(
                command=command,
                exit_code=255,
                stdout="",
                stderr="Phase 4 SSH executor requires private_key_path for non-interactive execution.",
                duration_seconds=round(time.monotonic() - start, 3),
                started_at=started_at,
                finished_at=finished_at,
                timed_out=False,
                failure_class="ssh_auth_failed",
            )

        ssh_command = [
            self.connection.ssh_binary,
            "-i",
            str(Path(self.connection.private_key_path)),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"UserKnownHostsFile={self.connection.known_hosts_path or 'NUL'}",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(self.connection.port),
            f"{self.connection.username}@{self.connection.host}",
            command,
        ]
        try:
            proc = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            duration = round(time.monotonic() - start, 3)
            stderr = _redact(proc.stderr or "", redact_patterns)
            stdout = _redact(proc.stdout or "", redact_patterns)
            failure_class = classify_failure(command, proc.returncode, stderr)
            return SSHCommandResult(
                command=command,
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                started_at=started_at,
                finished_at=_utc_now(),
                timed_out=False,
                failure_class=failure_class,
            )
        except subprocess.TimeoutExpired as exc:
            duration = round(time.monotonic() - start, 3)
            stderr = _redact((exc.stderr or "") if isinstance(exc.stderr, str) else "", redact_patterns)
            stdout = _redact((exc.stdout or "") if isinstance(exc.stdout, str) else "", redact_patterns)
            return SSHCommandResult(
                command=command,
                exit_code=124,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                started_at=started_at,
                finished_at=_utc_now(),
                timed_out=True,
                failure_class="command_timeout",
            )
