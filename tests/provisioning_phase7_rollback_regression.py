#!/usr/bin/env python3
"""Regression checks for Phase 7 rollback and failure reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provisioning.adapters.base import ProviderState  # noqa: E402
from provisioning.rollback import ProvisioningRollbackManager  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


@dataclass
class FakeAdapter:
    exists: bool = True
    destroy_raises: bool = False
    destroyed: list[str] = field(default_factory=list)

    def get_instance_state(self, instance_id: str) -> ProviderState:
        return ProviderState(
            instance_id=instance_id,
            exists=self.exists,
            power_state="running" if self.exists else "missing",
            provider_status="running" if self.exists else "not_found",
            raw={},
        )

    def destroy_instance(self, instance_id: str) -> str:
        if self.destroy_raises:
            raise RuntimeError("provider destroy failed")
        self.destroyed.append(instance_id)
        self.exists = False
        return "destroyed"


def main() -> int:
    failures: list[bool] = []

    no_instance = ProvisioningRollbackManager(FakeAdapter()).handle_failure(
        session_id="session-1",
        phase="provisioning",
        failed_step="create_instance",
        failure_class="provider_unavailable",
        message="VirtualBox is unavailable.",
    )
    failures.extend(
        [
            check("no instance rollback is not needed", no_instance.rollback.status == "not_needed"),
            check("no instance action is none", no_instance.rollback.action == "none"),
        ]
    )

    adapter = FakeAdapter(exists=True)
    destroyed_report = ProvisioningRollbackManager(adapter).handle_failure(
        session_id="session-2",
        phase="bootstrapping",
        failed_step="install_nginx",
        failure_class="package_manager_failed",
        message="nginx install failed.",
        instance_id="ava-web-001",
    )
    failures.extend(
        [
            check("default rollback destroys partial VM", destroyed_report.rollback.status == "destroyed"),
            check("destroy action is recorded", destroyed_report.rollback.action == "destroy_partial_vm"),
            check("adapter destroy was called", adapter.destroyed == ["ava-web-001"]),
            check("failure report preserves failed step", destroyed_report.failed_step == "install_nginx"),
            check("failure report preserves failure class", destroyed_report.failure_class == "package_manager_failed"),
        ]
    )

    missing_report = ProvisioningRollbackManager(FakeAdapter(exists=False)).handle_failure(
        session_id="session-3",
        phase="verifying",
        failed_step="host_http_200",
        failure_class="verification_failed",
        message="HTTP did not return 200.",
        instance_id="ava-web-missing",
    )
    failures.extend(
        [
            check("missing VM cleanup is already clean", missing_report.rollback.status == "not_needed"),
            check("missing VM destroy is not attempted", missing_report.rollback.action == "none"),
        ]
    )

    retain_adapter = FakeAdapter(exists=True)
    retained_report = ProvisioningRollbackManager(retain_adapter).handle_failure(
        session_id="session-4",
        phase="bootstrapping",
        failed_step="ssh_connect",
        failure_class="ssh_connect_timeout",
        message="SSH timed out.",
        instance_id="ava-web-retain",
        retain_for_debug=True,
    )
    failures.extend(
        [
            check("explicit debug retain keeps VM", retained_report.rollback.status == "retained"),
            check("debug retain action is recorded", retained_report.rollback.action == "retain_for_debug"),
            check("debug retain does not destroy", retain_adapter.destroyed == []),
        ]
    )

    failed_destroy_report = ProvisioningRollbackManager(FakeAdapter(exists=True, destroy_raises=True)).handle_failure(
        session_id="session-5",
        phase="provisioning",
        failed_step="network_config",
        failure_class="network_configuration_failed",
        message="NAT forwarding failed.",
        instance_id="ava-web-failed-destroy",
    )
    failures.extend(
        [
            check("destroy exception reports rollback failure", failed_destroy_report.rollback.status == "failed"),
            check("destroy exception keeps destroy action", failed_destroy_report.rollback.action == "destroy_partial_vm"),
            check("destroy exception evidence is user safe", "provider destroy failed" in failed_destroy_report.rollback.evidence),
        ]
    )

    as_dict = destroyed_report.to_dict()
    failures.extend(
        [
            check("report dict includes rollback", isinstance(as_dict.get("rollback"), dict)),
            check("report dict includes timestamp", bool(as_dict.get("timestamp"))),
            check("rollback dict includes evidence", bool(as_dict["rollback"].get("evidence"))),
        ]
    )

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nProvisioning Phase 7 rollback regression failed: {failed} issue(s)")
        return 1
    print("\nProvisioning Phase 7 rollback regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
