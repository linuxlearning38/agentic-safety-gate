"""Evidence-backed verification for AVA v2 provisioning."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import urlopen

from provisioning.adapters.base import ConnectionInfo, ProviderAdapter
from provisioning.bootstrap import SSHExecutor


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class VerificationCheck:
    """One verification check with evidence."""

    name: str
    passed: bool
    evidence: str
    timestamp: str = field(default_factory=_utc_now)
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class VerificationReport:
    """Final verification report for a provisioned instance."""

    instance_id: str
    status: str
    checks: list[VerificationCheck]
    timestamp: str = field(default_factory=_utc_now)

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "status": self.status,
            "timestamp": self.timestamp,
            "checks": [check.to_dict() for check in self.checks],
        }


class VerificationEngine:
    """Run provider, SSH, service, and HTTP checks before reporting success."""

    def __init__(
        self,
        adapter: ProviderAdapter,
        executor_factory: Callable[[ConnectionInfo], SSHExecutor],
        http_getter: Callable[[str, int], tuple[int, str]] | None = None,
    ):
        self.adapter = adapter
        self.executor_factory = executor_factory
        self.http_getter = http_getter or self._default_http_getter

    def verify_web_server(self, instance_id: str) -> VerificationReport:
        checks: list[VerificationCheck] = []

        state = self.adapter.get_instance_state(instance_id)
        checks.append(
            VerificationCheck(
                name="vm_exists",
                passed=state.exists,
                evidence=f"provider_status={state.provider_status}",
                failure_class=None if state.exists else "verification_failed",
            )
        )
        if not state.exists:
            return self._report(instance_id, checks)

        checks.append(
            VerificationCheck(
                name="vm_running",
                passed=state.power_state == "running",
                evidence=f"power_state={state.power_state}",
                failure_class=None if state.power_state == "running" else "verification_failed",
            )
        )
        if state.power_state != "running":
            return self._report(instance_id, checks)

        connection = self.adapter.get_connection_info(instance_id)
        checks.append(
            VerificationCheck(
                name="connection_info",
                passed=bool(connection.host and connection.port and connection.username),
                evidence=f"{connection.username}@{connection.host}:{connection.port}",
                failure_class=None if connection.host and connection.port and connection.username else "verification_failed",
            )
        )
        if not (connection.host and connection.port and connection.username):
            return self._report(instance_id, checks)

        executor = self.executor_factory(connection)
        ssh_result = executor.run("whoami", timeout_seconds=30)
        checks.append(
            VerificationCheck(
                name="ssh_command",
                passed=ssh_result.exit_code == 0,
                evidence=(ssh_result.stdout or ssh_result.stderr).strip(),
                failure_class=ssh_result.failure_class,
            )
        )
        if ssh_result.exit_code != 0:
            return self._report(instance_id, checks)

        nginx_result = executor.run("systemctl is-active nginx", timeout_seconds=30)
        checks.append(
            VerificationCheck(
                name="nginx_active",
                passed=nginx_result.exit_code == 0 and "active" in nginx_result.stdout.strip().lower(),
                evidence=(nginx_result.stdout or nginx_result.stderr).strip(),
                failure_class=nginx_result.failure_class,
            )
        )
        if checks[-1].passed is False:
            return self._report(instance_id, checks)

        local_http = executor.run("curl -fsS http://127.0.0.1/ >/dev/null", timeout_seconds=30)
        checks.append(
            VerificationCheck(
                name="guest_http_200",
                passed=local_http.exit_code == 0,
                evidence=(local_http.stdout or local_http.stderr or "guest curl returned success").strip(),
                failure_class=local_http.failure_class,
            )
        )
        if local_http.exit_code != 0:
            return self._report(instance_id, checks)

        http_port = connection.metadata.get("http_host_port")
        http_url = f"http://127.0.0.1:{http_port}/" if http_port else ""
        if not http_port:
            checks.append(
                VerificationCheck(
                    name="host_http_200",
                    passed=False,
                    evidence="missing http_host_port metadata",
                    failure_class="verification_failed",
                )
            )
            return self._report(instance_id, checks)

        status_code, body_hint = self.http_getter(http_url, 10)
        checks.append(
            VerificationCheck(
                name="host_http_200",
                passed=status_code == 200,
                evidence=f"{http_url} -> HTTP {status_code}; {body_hint}",
                failure_class=None if status_code == 200 else "verification_failed",
            )
        )
        return self._report(instance_id, checks)

    def _report(self, instance_id: str, checks: list[VerificationCheck]) -> VerificationReport:
        status = "passed" if checks and all(check.passed for check in checks) else "failed"
        return VerificationReport(instance_id=instance_id, status=status, checks=checks)

    @staticmethod
    def _default_http_getter(url: str, timeout: int) -> tuple[int, str]:
        try:
            with urlopen(url, timeout=timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                body = response.read(120).decode("utf-8", errors="replace")
                return int(status), body.replace("\n", " ")[:120]
        except Exception as exc:
            return 0, str(exc)
