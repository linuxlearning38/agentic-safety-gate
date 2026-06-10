"""Result writer facade for the AVA v2 host-side runner."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .job_queue import ProvisioningJobProgress, ProvisioningJobQueue, ProvisioningJobResult


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvisioningResultWriter:
    """Write runner lifecycle status and final results without leaking secrets."""

    def __init__(self, queue: ProvisioningJobQueue):
        self.queue = queue

    def status(self, job_id: str, status: str) -> None:
        self.queue.write_status(job_id, status)

    def progress(
        self,
        *,
        job_id: str,
        session_id: str,
        status: str,
        stage: str,
        instance_id: str | None = None,
        instance_name: str | None = None,
        ssh_host: str | None = None,
        ssh_port: int | None = None,
        http_port: int | None = None,
        runner_key_path: str | None = None,
        message: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> ProvisioningJobProgress:
        self.queue.write_status(job_id, status)
        progress = ProvisioningJobProgress(
            job_id=job_id,
            session_id=session_id,
            status=status,
            stage=stage,
            instance_id=instance_id,
            instance_name=instance_name,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            http_port=http_port,
            runner_key_path=runner_key_path,
            message=message,
            error=_redact_error(error) if error else None,
        )
        write_progress = getattr(self.queue, "write_progress", None)
        if write_progress is not None:
            write_progress(progress)
        return progress

    def completed(
        self,
        *,
        job_id: str,
        session_id: str = "",
        instance_id: str,
        instance_name: str,
        ssh_host: str,
        ssh_port: int,
        http_port: int | None,
        verification_evidence: dict[str, Any],
    ) -> ProvisioningJobResult:
        self.progress(
            job_id=job_id,
            session_id=session_id,
            status="completed",
            stage="completed",
            instance_id=instance_id,
            instance_name=instance_name,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            http_port=http_port,
            message="VM created, bootstrapped, hardened, and verified.",
        )
        result = ProvisioningJobResult(
            job_id=job_id,
            instance_id=instance_id,
            instance_name=instance_name,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            http_port=http_port,
            verification_evidence=verification_evidence,
            completion_timestamp=_utc_now(),
            error=None,
        )
        self.queue.write_result(result)
        return result

    def failed(
        self,
        *,
        job_id: str,
        session_id: str = "",
        instance_id: str | None,
        instance_name: str | None,
        error: dict[str, Any],
        verification_evidence: dict[str, Any] | None = None,
    ) -> ProvisioningJobResult:
        self.progress(
            job_id=job_id,
            session_id=session_id,
            status="failed",
            stage="failed",
            instance_id=instance_id,
            instance_name=instance_name,
            message="Provisioning failed before AVA received a terminal success result.",
            error=error,
        )
        result = ProvisioningJobResult(
            job_id=job_id,
            instance_id=instance_id,
            instance_name=instance_name,
            verification_evidence=verification_evidence or {},
            completion_timestamp=_utc_now(),
            error=_redact_error(error),
        )
        self.queue.write_result(result)
        return result


def _redact_error(error: dict[str, Any]) -> dict[str, Any]:
    safe = dict(error or {})
    secret_values = {
        str(value)
        for key, value in safe.items()
        if key in {"temporary_password", "password", "user_data", "cloud_init_user_data"} and value
    }
    for key in ("temporary_password", "password", "user_data", "cloud_init_user_data"):
        if key in safe:
            safe[key] = "[REDACTED]"
    for key, value in list(safe.items()):
        if isinstance(value, str):
            for secret in secret_values:
                value = value.replace(secret, "[REDACTED]")
            safe[key] = value
    return safe
