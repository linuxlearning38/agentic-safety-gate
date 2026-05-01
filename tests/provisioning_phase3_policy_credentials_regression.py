#!/usr/bin/env python3
"""Regression checks for v2 Phase 3 policy, approval, and credentials."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control import approval  # noqa: E402
from provisioning.conversation import ProvisioningFlowEngine, SessionManager, SessionPhase  # noqa: E402
from provisioning.credentials import CredentialManager  # noqa: E402
from provisioning.policy import evaluate_provisioning_policy  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


def main() -> int:
    failures: list[bool] = []
    temp_dir = tempfile.mkdtemp(prefix="ava-phase3-policy-")
    old_queue_path = os.environ.get("APPROVAL_QUEUE_PATH")
    try:
        os.environ["APPROVAL_QUEUE_PATH"] = str(Path(temp_dir) / "approval_queue.json")
        session_db = Path(temp_dir) / "sessions.sqlite3"
        credential_db = Path(temp_dir) / "credentials.sqlite3"
        manager = SessionManager(session_db)
        credentials = CredentialManager(credential_db)
        engine = ProvisioningFlowEngine(manager, credentials=credentials)

        response = engine.start("user-1", "I want a web server in Ubuntu")
        ready = engine.submit_answers(
            response.session.session_id,
            {"cpu": "2", "ram_gb": "4", "disk_gb": "30"},
        )
        failures.extend(
            [
                check("complete specs await approval", ready.session.phase == SessionPhase.AWAITING_APPROVAL),
                check("ready response requires approval", ready.requires_approval is True),
            ]
        )

        policy_decision = evaluate_provisioning_policy(ready.session.desired_state)
        failures.extend(
            [
                check("policy requires approval", policy_decision.effect == "require_approval"),
                check("policy marks provisioning medium risk", policy_decision.risk == "medium"),
            ]
        )

        approval_response = engine.request_approval(ready.session.session_id)
        queued = approval.get_by_id(approval_response.approval_id)
        failures.extend(
            [
                check("approval id returned", bool(approval_response.approval_id), str(approval_response.approval_id)),
                check("approval queue entry exists", queued is not None),
                check("approval metadata records session", queued["metadata"]["session_id"] == ready.session.session_id),
                check("approval status starts pending", queued["status"] == "pending"),
            ]
        )

        pending = engine.continue_after_approval(ready.session.session_id)
        failures.extend(
            [
                check("execution blocked while approval pending", pending.error == "approval_pending"),
                check("session remains awaiting approval", pending.session.phase == SessionPhase.AWAITING_APPROVAL),
            ]
        )

        approval.update_status(approval_response.approval_id, "approved")
        allowed = engine.continue_after_approval(ready.session.session_id)
        credential = allowed.credential
        failures.extend(
            [
                check("approval moves to first-login checkpoint", allowed.session.phase == SessionPhase.AWAITING_FIRST_LOGIN),
                check("credential issued once", credential is not None and bool(credential.temporary_password)),
                check("credential id stored on session", allowed.session.credential_id == credential.credential_id),
                check("username stored for later cloud-init use", allowed.session.collected_answers.get("username") == "avaadmin"),
            ]
        )

        display_record = credentials.get_display_record(credential.credential_id)
        failures.extend(
            [
                check("credential metadata can be reloaded", display_record is not None),
                check("credential password is not recoverable after issue", display_record.temporary_password is None),
                check("credential hash verifies original password", credentials.verify_password(credential.credential_id, credential.temporary_password)),
            ]
        )

        replay = engine.continue_after_approval(ready.session.session_id)
        failures.extend(
            [
                check("credential is not reissued after phase advances", replay.error == "approval_not_expected"),
                check("credential replay does not expose password", replay.credential is None),
            ]
        )

        confirmed = engine.confirm_first_login(ready.session.session_id)
        failures.append(
            check("first-login confirmation advances phase", confirmed.session.phase == SessionPhase.AWAITING_POST_LOGIN_CHOICES)
        )

        blocked = evaluate_provisioning_policy(
            {
                "provider": "virtualbox",
                "os": "ubuntu",
                "role": "database",
                "cpu": 2,
                "ram_gb": 4,
                "disk_gb": 30,
            }
        )
        failures.extend(
            [
                check("unsupported desired state is blocked", blocked.effect == "block"),
                check("blocked state does not require approval", blocked.approval_required is False),
            ]
        )
    finally:
        if old_queue_path is None:
            os.environ.pop("APPROVAL_QUEUE_PATH", None)
        else:
            os.environ["APPROVAL_QUEUE_PATH"] = old_queue_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nProvisioning Phase 3 regression failed: {failed} issue(s)")
        return 1
    print("\nProvisioning Phase 3 regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
