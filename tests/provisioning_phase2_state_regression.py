#!/usr/bin/env python3
"""Regression checks for v2 Phase 2 desired-state and session behavior."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provisioning.conversation import ProvisioningFlowEngine, SessionManager, SessionPhase  # noqa: E402
from provisioning.desired_state import DesiredState, DesiredStateError, missing_required_specs  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


def main() -> int:
    failures: list[bool] = []
    temp_dir = tempfile.mkdtemp(prefix="ava-phase2-state-")
    try:
        db_path = Path(temp_dir) / "sessions.sqlite3"
        manager = SessionManager(db_path)
        engine = ProvisioningFlowEngine(manager)

        response = engine.start("user-1", "I want a web server in Ubuntu")
        failures.extend(
            [
                check("web request starts awaiting specs", response.session.phase == SessionPhase.AWAITING_SPECS),
                check("role inferred as web_server", response.session.role == "web_server"),
                check("missing specs are requested", response.missing_fields == ["cpu", "ram_gb", "disk_gb"], str(response.missing_fields)),
            ]
        )

        reloaded_manager = SessionManager(db_path)
        reloaded = reloaded_manager.require(response.session.session_id)
        failures.extend(
            [
                check("session survives manager reload", reloaded.session_id == response.session.session_id),
                check("collected answers survive reload", reloaded.collected_answers.get("role") == "web_server"),
            ]
        )

        ready = ProvisioningFlowEngine(reloaded_manager).submit_answers(
            response.session.session_id,
            {
                "cpu": "2",
                "ram_gb": "4",
                "disk_gb": "30",
                "hostname": "AVA_WEB_01",
            },
        )
        failures.extend(
            [
                check("complete specs move to approval", ready.session.phase == SessionPhase.AWAITING_APPROVAL),
                check("approval is required", ready.requires_approval is True),
                check("desired state is ready", ready.desired_state_ready is True),
                check("desired state provider defaulted", ready.session.desired_state.get("provider") == "virtualbox"),
                check("desired state hardening defaulted", ready.session.desired_state.get("hardening_profile") == "baseline_linux"),
                check("desired state hostname normalized", ready.session.desired_state.get("vm_name") == "ava-web-01"),
            ]
        )

        cancel_response = engine.start("user-2", "I want a VM")
        cancelled = engine.cancel(cancel_response.session.session_id)
        failures.extend(
            [
                check("generic VM request asks for type", cancel_response.session.phase == SessionPhase.AWAITING_VM_TYPE),
                check("cancel persists terminal phase", cancelled.session.phase == SessionPhase.CANCELLED),
                check("cancelled session is not active", not manager.list_active("user-2")),
            ]
        )

        invalid = engine.start("user-3", "I want a web server in Ubuntu")
        invalid_ready = engine.submit_answers(
            invalid.session.session_id,
            {
                "cpu": "0",
                "ram_gb": "4",
                "disk_gb": "30",
            },
        )
        failures.extend(
            [
                check("invalid specs stay awaiting specs", invalid_ready.session.phase == SessionPhase.AWAITING_SPECS),
                check("invalid cpu remains missing", "cpu" in invalid_ready.missing_fields),
            ]
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    try:
        DesiredState.from_answers({"cpu": 2, "ram_gb": 4, "disk_gb": 30, "role": "database"})
        failures.append(check("unsupported role is rejected", False))
    except DesiredStateError:
        failures.append(check("unsupported role is rejected", True))

    try:
        DesiredState.from_answers({"cpu": 2, "ram_gb": 4, "disk_gb": 30, "hostname": "01-bad-host"})
        failures.append(check("invalid hostname is rejected", False))
    except DesiredStateError:
        failures.append(check("invalid hostname is rejected", True))

    failures.append(
        check(
            "missing required specs handles invalid integers",
            missing_required_specs({"cpu": "two", "ram_gb": 4}) == ["cpu", "disk_gb"],
        )
    )

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nProvisioning Phase 2 regression failed: {failed} issue(s)")
        return 1
    print("\nProvisioning Phase 2 regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
