#!/usr/bin/env python3
"""Regression checks for v2 Phase 5 verification and state persistence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provisioning.adapters.base import ConnectionInfo, ProviderState  # noqa: E402
from provisioning.bootstrap import SSHCommandResult  # noqa: E402
from provisioning.state import ProvisioningStateStore  # noqa: E402
from provisioning.verify import VerificationEngine  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


@dataclass
class FakeAdapter:
    exists: bool = True
    power_state: str = "running"
    http_port: int | None = 8080

    def get_instance_state(self, instance_id: str) -> ProviderState:
        return ProviderState(
            instance_id=instance_id,
            exists=self.exists,
            power_state=self.power_state,
            provider_status=self.power_state,
            raw={"VMState": self.power_state},
        )

    def get_connection_info(self, instance_id: str) -> ConnectionInfo:
        return ConnectionInfo(
            username="avaadmin",
            host="127.0.0.1",
            port=2222,
            metadata={"http_host_port": self.http_port},
        )


class FakeExecutor:
    def __init__(self, responses: dict[str, SSHCommandResult]):
        self.responses = responses

    def run(self, command: str, *, timeout_seconds: int = 120, redact_patterns=()):
        for key, response in self.responses.items():
            if key in command:
                return response
        return result(command, 0, stdout="ok")


def result(command: str, exit_code: int, stdout: str = "", stderr: str = "", failure_class: str | None = None) -> SSHCommandResult:
    return SSHCommandResult(
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        started_at="2026-05-01T00:00:00+00:00",
        finished_at="2026-05-01T00:00:01+00:00",
        timed_out=False,
        failure_class=failure_class,
    )


def main() -> int:
    failures: list[bool] = []

    success_executor = FakeExecutor(
        {
            "whoami": result("whoami", 0, stdout="avaadmin"),
            "systemctl is-active nginx": result("systemctl is-active nginx", 0, stdout="active\n"),
            "curl -fsS": result("curl -fsS http://127.0.0.1/", 0, stdout=""),
        }
    )
    success_engine = VerificationEngine(
        FakeAdapter(),
        executor_factory=lambda connection: success_executor,
        http_getter=lambda url, timeout: (200, "Welcome to nginx"),
    )
    success_report = success_engine.verify_web_server("ava-web-001")
    failures.extend(
        [
            check("success report passes", success_report.passed is True),
            check("success report has required checks", [item.name for item in success_report.checks] == [
                "vm_exists",
                "vm_running",
                "connection_info",
                "ssh_command",
                "nginx_active",
                "guest_http_200",
                "host_http_200",
            ]),
            check("success requires evidence for every check", all(item.evidence for item in success_report.checks)),
        ]
    )

    failed_engine = VerificationEngine(
        FakeAdapter(),
        executor_factory=lambda connection: success_executor,
        http_getter=lambda url, timeout: (502, "bad gateway"),
    )
    failed_report = failed_engine.verify_web_server("ava-web-002")
    failures.extend(
        [
            check("failed HTTP report fails", failed_report.passed is False),
            check("failed HTTP has clean failure class", failed_report.checks[-1].failure_class == "verification_failed"),
        ]
    )

    stopped_report = VerificationEngine(
        FakeAdapter(power_state="poweroff"),
        executor_factory=lambda connection: success_executor,
    ).verify_web_server("ava-web-003")
    failures.extend(
        [
            check("stopped VM report fails", stopped_report.passed is False),
            check("stopped VM stops before SSH checks", [item.name for item in stopped_report.checks] == ["vm_exists", "vm_running"]),
        ]
    )

    temp_dir = tempfile.mkdtemp(prefix="ava-phase5-state-")
    try:
        store = ProvisioningStateStore(Path(temp_dir) / "state.sqlite3")
        record = store.save_verification(
            session_id="session-1",
            desired_state={"role": "web_server"},
            actual_state={"power_state": "running"},
            verification_report=success_report,
        )
        loaded = store.get("ava-web-001")
        failures.extend(
            [
                check("success outcome persists as completed", record.outcome == "completed"),
                check("record reloads by instance id", loaded is not None and loaded.instance_id == "ava-web-001"),
                check("verification evidence persists", loaded.verification_result["status"] == "passed"),
            ]
        )
        failed_record = store.save_verification(
            session_id="session-2",
            desired_state={"role": "web_server"},
            actual_state={"power_state": "running"},
            verification_report=failed_report,
        )
        failures.append(check("failed outcome persists as failed", failed_record.outcome == "failed"))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nProvisioning Phase 5 regression failed: {failed} issue(s)")
        return 1
    print("\nProvisioning Phase 5 regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
