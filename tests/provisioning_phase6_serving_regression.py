#!/usr/bin/env python3
"""Regression checks for Phase 6 guided provisioning serving integration."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control import approval  # noqa: E402
from control.input_router import route_query  # noqa: E402
from provisioning.conversation import SessionPhase  # noqa: E402
from provisioning.serving import ProvisioningChatService  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


def main() -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="ava-phase6-serving-"))
    old_queue = os.environ.get("APPROVAL_QUEUE_PATH")
    os.environ["APPROVAL_QUEUE_PATH"] = str(temp_dir / "approval_queue.json")
    try:
        service = ProvisioningChatService(temp_dir / "provisioning_sessions.sqlite3")
        failures: list[bool] = []

        start_route = route_query("I want a web server in Ubuntu")
        failures.append(check("router detects provisioning intent", start_route.intent == "provisioning", start_route.reason))

        start = service.handle("user-1", "I want a web server in Ubuntu", route_intent=start_route.intent)
        start_session = service.sessions.list_active("user-1")[0]
        failures.extend(
            [
                check("start request is handled", start.handled),
                check("start asks for missing specs", "cpu" in start.response.lower() and "ram" in start.response.lower()),
                check("start phase awaits specs", start_session.phase == SessionPhase.AWAITING_SPECS, start_session.phase.value),
            ]
        )

        specs = service.handle("user-1", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        approval_id = specs.metadata["provisioning"]["approval_id"]
        spec_session = service.sessions.list_active("user-1")[0]
        failures.extend(
            [
                check("spec answer is handled", specs.handled),
                check("spec answer queues approval", bool(approval_id), str(approval_id)),
                check("approval phase is persisted", spec_session.phase == SessionPhase.AWAITING_APPROVAL, spec_session.phase.value),
                check("desired state records cpu", spec_session.desired_state.get("cpu") == 2),
                check("desired state records ram", spec_session.desired_state.get("ram_gb") == 4),
                check("desired state records disk", spec_session.desired_state.get("disk_gb") == 30),
            ]
        )

        start_alt = service.handle("user-alt", "I want a web server", route_intent="provisioning")
        alt_specs = service.handle("user-alt", "3 CPU, 8gb RAM, 40 GB disk", route_intent=None)
        alt_session = service.sessions.list_active("user-alt")[0]
        failures.extend(
            [
                check("alternate start is handled", start_alt.handled),
                check("uppercase cpu spec answer is handled", alt_specs.handled),
                check("uppercase cpu spec queues approval", bool(alt_specs.metadata["provisioning"]["approval_id"])),
                check("uppercase cpu spec records cpu", alt_session.desired_state.get("cpu") == 3),
                check("uppercase cpu spec records ram", alt_session.desired_state.get("ram_gb") == 8),
                check("uppercase cpu spec records disk", alt_session.desired_state.get("disk_gb") == 40),
            ]
        )

        start_lower = service.handle("user-lower", "I want a web server", route_intent="provisioning")
        lower_specs = service.handle("user-lower", "3 cpu, 8gb ram, 60 gb disk", route_intent=None)
        lower_session = service.sessions.list_active("user-lower")[0]
        failures.extend(
            [
                check("lowercase cpu start is handled", start_lower.handled),
                check("lowercase cpu spec answer is handled", lower_specs.handled),
                check("lowercase cpu spec queues approval", bool(lower_specs.metadata["provisioning"]["approval_id"])),
                check("lowercase cpu spec records cpu", lower_session.desired_state.get("cpu") == 3),
                check("lowercase cpu spec records ram", lower_session.desired_state.get("ram_gb") == 8),
                check("lowercase cpu spec records disk", lower_session.desired_state.get("disk_gb") == 60),
            ]
        )

        pending = service.handle("user-1", "continue provisioning", route_intent=None)
        failures.extend(
            [
                check("pending approval continuation is handled", pending.handled),
                check("pending approval does not expose credential", "temporary password" not in pending.response.lower()),
            ]
        )

        wrong_approval = service.handle("user-1", "approve deadbeef", route_intent=None)
        failures.extend(
            [
                check("wrong chat approval id is handled", wrong_approval.handled),
                check("wrong chat approval id is rejected", "does not match" in wrong_approval.response.lower()),
                check("wrong chat approval does not approve queue", approval.get_by_id(approval_id).get("status") == "pending"),
            ]
        )

        approved = service.handle("user-1", f"approve - {approval_id}", route_intent=None)
        approved_session = service.sessions.list_active("user-1")[0]
        failures.extend(
            [
                check("chat approval with separator is handled", approved.handled),
                check("chat approval updates queue", approval.get_by_id(approval_id).get("status") == "approved"),
                check("chat approval issues one-time credential", "temporary password" in approved.response.lower()),
                check("chat approval clarifies runner boundary", "host-side" in approved.response.lower()),
                check("approved phase awaits first login", approved_session.phase == SessionPhase.AWAITING_FIRST_LOGIN, approved_session.phase.value),
            ]
        )

        login = service.handle("user-1", "I logged in and changed the password", route_intent=None)
        login_session = service.sessions.list_active("user-1")[0]
        failures.extend(
            [
                check("first-login confirmation is handled", login.handled),
                check("first-login phase awaits hardening", login_session.phase == SessionPhase.AWAITING_POST_LOGIN_CHOICES, login_session.phase.value),
                check("hardening default is explained", "baseline_linux" in login.response),
            ]
        )

        hardening = service.handle("user-1", "yes harden it", route_intent=None)
        hardening_session = service.sessions.list_active("user-1")[0]
        failures.extend(
            [
                check("hardening choice is handled", hardening.handled),
                check("hardening moves to bootstrapping checkpoint", hardening_session.phase == SessionPhase.BOOTSTRAPPING, hardening_session.phase.value),
                check("post-login action recorded", hardening_session.collected_answers.get("post_login_actions") == ["baseline_linux"]),
            ]
        )

        unrelated = service.handle("user-2", "What is Kubernetes?", route_intent=route_query("What is Kubernetes?").intent)
        failures.append(check("unrelated knowledge prompt is not hijacked", unrelated.handled is False))

        diagram_route = route_query("ava linux provisioning diagram")
        failures.append(check("provisioning diagram stays architecture/diagram", diagram_route.intent == "architecture", diagram_route.intent or "none"))

        failed = len([item for item in failures if not item])
        if failed:
            print(f"\nProvisioning Phase 6 serving regression failed: {failed} issue(s)")
            return 1
        print("\nProvisioning Phase 6 serving regression passed.")
        return 0
    finally:
        if old_queue is None:
            os.environ.pop("APPROVAL_QUEUE_PATH", None)
        else:
            os.environ["APPROVAL_QUEUE_PATH"] = old_queue
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
