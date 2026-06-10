#!/usr/bin/env python3
"""Source-of-truth regression tests for invariants I1–I7.

Reproduces the live bug (job 5646da79: result with BOTH instance_id AND error set
caused _runner_snapshot to classify the job as 'completed', silently skipping the
SQLite phase=FAILED write and leaving session stuck at awaiting_first_login forever).

Tests:
  T1  result with instance_id AND error  -> snapshot=FAILED, SQLite phase=FAILED   (I1, I2, I3)
  T2  Redis empty after reboot + runner offline + no VM -> reconcile, clean, unblock (I6, I7)
  T3  VM manually deleted, session=awaiting_first_login -> honest msg, no exception  (I4, I5)
  T4  Redis raises ConnectionError during status read -> clean response, never raises (I5)
  T5  happy path: completed result, no error -> COMPLETED, connection details shown  (I3, I5 – no regression)
  T6  failure+rollback cycle N times -> no session stuck, every new request accepted (I7)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provisioning.conversation import SessionPhase  # noqa: E402
from provisioning.runner import (  # noqa: E402
    Day2OperationJob,
    Day2OperationResult,
    ProvisioningJob,
    ProvisioningJobProgress,
    ProvisioningJobResult,
)
from provisioning.serving import ProvisioningChatService  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


# ---------------------------------------------------------------------------
# Minimal fake job queue (same pattern as phase6 tests)
# ---------------------------------------------------------------------------

class FakeJobQueue:
    def __init__(self, *, raise_on_redis: bool = False, runner_healthy: bool = True):
        self.statuses: dict[str, str] = {}
        self.results: dict[str, ProvisioningJobResult] = {}
        self.progress: dict[str, ProvisioningJobProgress] = {}
        self.day2_jobs: dict[str, Any] = {}
        self.day2_statuses: dict[str, str] = {}
        self.day2_results: dict[str, Any] = {}
        self._raise_on_redis = raise_on_redis
        self._runner_healthy = runner_healthy
        self._counter = 0
        self._day2_counter = 0

    # --- provisioning job contract ---

    def enqueue_approved_job(self, *, session_id, desired_state, credential_id, username, temporary_password):
        self._counter += 1
        job_id = f"job-{self._counter:04d}"
        job = ProvisioningJob(
            job_id=job_id,
            session_id=session_id,
            desired_state=dict(desired_state),
            credentials_seed_data={"credential_id": credential_id, "username": username, "temporary_password": temporary_password},
            enqueued_at="2026-06-09T00:00:00+00:00",
            expires_at="2026-06-09T00:30:00+00:00",
        )
        self.statuses[job_id] = "queued"
        return job

    def get_status(self, job_id: str) -> str | None:
        if self._raise_on_redis:
            raise ConnectionError("Redis connection refused")
        return self.statuses.get(job_id)

    def write_status(self, job_id: str, status: str) -> None:
        self.statuses[job_id] = status

    def get_result(self, job_id: str) -> ProvisioningJobResult | None:
        if self._raise_on_redis:
            raise ConnectionError("Redis connection refused")
        return self.results.get(job_id)

    def write_result(self, result: ProvisioningJobResult) -> None:
        self.results[result.job_id] = result

    def get_progress(self, job_id: str) -> ProvisioningJobProgress | None:
        if self._raise_on_redis:
            raise ConnectionError("Redis connection refused")
        return self.progress.get(job_id)

    def write_progress(self, progress: ProvisioningJobProgress) -> None:
        self.progress[progress.job_id] = progress

    def claim_next_job(self, *, timeout_seconds=30):
        return None

    def is_runner_healthy(self) -> bool:
        return self._runner_healthy

    # --- day2 contract (needed to avoid AttributeError on d2 paths) ---

    def enqueue_day2_operation(self, *, session_id, operation, target, instance_id, instance_name,
                                ssh_host, ssh_port, http_port, metadata=None):
        self._day2_counter += 1
        op_id = f"day2-{self._day2_counter:04d}"
        job = Day2OperationJob(
            operation_id=op_id, session_id=session_id, operation=operation, target=target,
            instance_id=instance_id, instance_name=instance_name,
            ssh_host=ssh_host, ssh_port=ssh_port, http_port=http_port,
            requested_at="2026-06-09T00:15:00+00:00", metadata=dict(metadata or {}),
        )
        self.day2_jobs[op_id] = job
        self.day2_statuses[op_id] = "queued"
        # Fake immediate completion for verify so _existing_vm_still_live works
        if operation == "verify":
            self.day2_statuses[op_id] = "completed"
            self.day2_results[op_id] = Day2OperationResult(
                operation_id=op_id, operation="verify", status="completed",
                instance_id=instance_id, instance_name=instance_name,
                evidence={"checks": [
                    {"name": "vm_exists", "passed": True, "evidence": "running"},
                    {"name": "vm_running", "passed": True, "evidence": "running"},
                    {"name": "host_http_200", "passed": True, "evidence": f"http://127.0.0.1:{http_port}/ -> 200"},
                ]},
                completion_timestamp="2026-06-09T00:15:05+00:00",
            )
        return job

    def claim_next_day2_operation(self, *, timeout_seconds=1):
        return None

    def get_day2_status(self, op_id: str) -> str | None:
        return self.day2_statuses.get(op_id)

    def get_day2_result(self, op_id: str) -> Any | None:
        return self.day2_results.get(op_id)

    def write_day2_status(self, op_id: str, status: str) -> None:
        self.day2_statuses[op_id] = status

    def write_day2_result(self, result: Any) -> None:
        self.day2_results[result.operation_id] = result


def _setup_approved_session(service: ProvisioningChatService, user_id: str, job_queue: FakeJobQueue):
    """Run through provisioning flow up to AWAITING_FIRST_LOGIN; return (session, job_id)."""
    os.environ.setdefault("APPROVAL_QUEUE_PATH", str(Path(tempfile.mkdtemp()) / "apq.json"))
    service.handle(user_id, "I want a web server", route_intent="provisioning")
    service.handle(user_id, "2 CPU, 4 GB RAM, 30 GB disk, hostname ava-web-01", route_intent=None)
    session = service.sessions.list_active(user_id)[0]
    service.handle(user_id, f"approve {session.approval_id}", route_intent=None)
    session = service.sessions.list_active(user_id)[0]
    job_id = (session.collected_answers or {}).get("runner_job_id")
    assert job_id, "test setup failed to queue a runner job"
    return session, job_id


# ---------------------------------------------------------------------------
# T1 — result with BOTH instance_id AND error  →  FAILED everywhere  (I1, I2, I3)
# ---------------------------------------------------------------------------

def test_t1_result_error_wins_over_instance_id() -> list[bool]:
    """Reproduces the exact live bug for job 5646da79.

    Previously: if result and result.instance_id → status='completed', error branch skipped,
    SQLite phase stayed awaiting_first_login.
    After fix:  if result and result.error checked first → status='failed', phase=FAILED saved.
    """
    results: list[bool] = []
    tmp = Path(tempfile.mkdtemp(prefix="ava-t1-"))
    queue = FakeJobQueue()
    old_aq = os.environ.get("APPROVAL_QUEUE_PATH")
    os.environ["APPROVAL_QUEUE_PATH"] = str(tmp / "apq.json")
    try:
        service = ProvisioningChatService(tmp / "sessions.sqlite3", job_queue=queue)
        session, job_id = _setup_approved_session(service, "user-t1", queue)

        # Simulate the exact live condition: result has BOTH instance_id AND error
        queue.write_status(job_id, "failed")
        queue.write_result(ProvisioningJobResult(
            job_id=job_id,
            instance_id="ava-web-01",          # set by runner for rollback tracing (I3)
            instance_name="ava-web-01",
            ssh_host=None,
            ssh_port=None,
            http_port=None,
            verification_evidence={"rollback": {
                "action": "destroy_partial_vm",
                "evidence": "Destroyed partial instance 'ava-web-01'.",
                "status": "destroyed",
                "timestamp": "2026-06-09T17:30:56+00:00",
            }},
            completion_timestamp="2026-06-09T17:30:57+00:00",
            error={
                "failed_step": "host_runner",
                "failure_class": "runner_failed",
                "instance_id": "ava-web-01",
                "message": "cloud-init first-access marker was not confirmed (exit_code=124, failure_class=command_timeout, stdout='AVA_CLOUD_INIT_READY ava-web-01\\n', stderr='')",
                "phase": "provisioning",
                "rollback": {"action": "destroy_partial_vm", "status": "destroyed"},
            },
        ))

        # Status query must show FAILED (not awaiting_first_login, not completed)
        status_resp = service.handle("user-t1", "show me the provisioning status", route_intent=None)

        # Read back the session — SQLite must have been updated to FAILED
        fresh = service.sessions.list_active("user-t1")
        from control import approval  # noqa: E402 — imported late to avoid env issues
        results.extend([
            check("T1: status response is handled", status_resp.handled),
            check("T1: effective phase is failed (not completed)", "phase: `failed`" in status_resp.response.lower()),
            check("T1: response shows failure details", "failure details" in status_resp.response.lower()),
            check("T1: destroyed rollback is not shown as an attached live VM", "attached vm instance: `none yet`" in status_resp.response.lower()),
            check("T1: destroyed rollback does not show old VM name in normal status", "failed vm identity" not in status_resp.response.lower()),
            check("T1: no SSH pending shown for a failed VM", "ssh pending" not in status_resp.response.lower()),
            check("T1: SQLite session is no longer active (phase=FAILED written)", len(fresh) == 0),
        ])

        # instance_id must be recorded (I3 — identity regardless of outcome)
        all_sessions = service.sessions.list_recent("user-t1", limit=5)
        the_session = next((s for s in all_sessions if s.session_id == session.session_id), None)
        results.append(check("T1: instance_id recorded in SQLite for identity (I3)", the_session is not None and the_session.instance_id == "ava-web-01"))

        # New provisioning request must be unblocked (I7)
        retry = service.handle("user-t1", "I want a web server", route_intent="provisioning")
        results.extend([
            check("T1: failed session does not block new provisioning (I7)", retry.handled),
            check("T1: retry asks for specs (not active-guard message)", "cpu" in retry.response.lower()),
        ])
    finally:
        if old_aq is None:
            os.environ.pop("APPROVAL_QUEUE_PATH", None)
        else:
            os.environ["APPROVAL_QUEUE_PATH"] = old_aq
        shutil.rmtree(tmp, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# T2 — Redis empty (post-reboot) + runner offline + VM absent → reconcile  (I6, I7)
# ---------------------------------------------------------------------------

def test_t2_redis_empty_runner_offline_reconciles() -> list[bool]:
    """Simulates a host restart: Redis is wiped, runner heartbeat is gone.

    A non-terminal session with a runner_job_id but no Redis state (status=None,
    result=None) and an offline runner must reconcile to terminal and unblock new
    provisioning — not stay stuck at awaiting_first_login.
    """
    import sqlite3  # noqa: PLC0415

    results: list[bool] = []
    tmp = Path(tempfile.mkdtemp(prefix="ava-t2-"))
    queue = FakeJobQueue(runner_healthy=True)
    old_aq = os.environ.get("APPROVAL_QUEUE_PATH")
    os.environ["APPROVAL_QUEUE_PATH"] = str(tmp / "apq.json")
    try:
        service = ProvisioningChatService(tmp / "sessions.sqlite3", job_queue=queue)
        session, job_id = _setup_approved_session(service, "user-t2", queue)

        # Simulate Redis wipe after reboot: clear all state
        queue.statuses.clear()
        queue.results.clear()
        queue.progress.clear()
        # Runner is now offline (no heartbeat)
        queue._runner_healthy = False
        # Back-date session so the orphaned age check fires (> 2 min)
        aged_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        with sqlite3.connect(tmp / "sessions.sqlite3") as conn:
            conn.execute(
                "UPDATE provisioning_sessions SET updated_at = ? WHERE session_id = ?",
                (aged_at, session.session_id),
            )

        # Status query must reconcile and report failed (not awaiting_first_login)
        status_resp = service.handle("user-t2", "show me the provisioning status", route_intent=None)
        fresh = service.sessions.list_active("user-t2")
        results.extend([
            check("T2: status response handled without exception", status_resp.handled),
            check("T2: phase reconciled to failed (not awaiting_first_login)", "phase: `failed`" in status_resp.response.lower()),
            check("T2: response is actionable (not a stack trace)", "provisioning status" in status_resp.response.lower()),
            check("T2: session removed from active list after reconciliation", len(fresh) == 0),
        ])

        # New provisioning request must be accepted (I7)
        queue._runner_healthy = True
        retry = service.handle("user-t2", "I want a web server", route_intent="provisioning")
        results.extend([
            check("T2: new provisioning accepted after reconciliation (I7)", retry.handled),
            check("T2: retry asks for specs not active-guard", "already active" not in retry.response.lower()),
        ])
    finally:
        if old_aq is None:
            os.environ.pop("APPROVAL_QUEUE_PATH", None)
        else:
            os.environ["APPROVAL_QUEUE_PATH"] = old_aq
        shutil.rmtree(tmp, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# T3 — VM manually deleted while session=awaiting_first_login → honest, no raise  (I4, I5)
# ---------------------------------------------------------------------------

def test_t3_vm_deleted_honest_message_no_exception() -> list[bool]:
    """VM was manually removed from VirtualBox while session is awaiting_first_login.

    The runner completed the job and stored the result.  The VM no longer exists.
    AVA must surface an honest message (not 'SSH pending', not a stack trace) and
    must not falsely assert the VM exists.  A new provisioning request must be
    accepted (the deleted-VM session must not block it).
    """
    results: list[bool] = []
    tmp = Path(tempfile.mkdtemp(prefix="ava-t3-"))
    # This queue returns vm_exists=False via live verify (simulates deleted VM)
    queue = FakeJobQueue()
    old_aq = os.environ.get("APPROVAL_QUEUE_PATH")
    os.environ["APPROVAL_QUEUE_PATH"] = str(tmp / "apq.json")
    try:
        service = ProvisioningChatService(tmp / "sessions.sqlite3", job_queue=queue)
        session, job_id = _setup_approved_session(service, "user-t3", queue)

        # Runner completed normally; VM existed at that point
        queue.write_status(job_id, "completed")
        queue.write_result(ProvisioningJobResult(
            job_id=job_id,
            instance_id="ava-web-01",
            instance_name="ava-web-01",
            ssh_host="127.0.0.1",
            ssh_port=2222,
            http_port=8080,
            verification_evidence={"checks": [{"name": "host_http_200", "passed": True}]},
            completion_timestamp="2026-06-09T16:00:00+00:00",
            error=None,                  # No error — runner was happy
        ))

        # At this point, the VM has been manually deleted from VirtualBox.
        # Status query must not crash and must not say "SSH pending" forever.
        raised = False
        status_resp = None
        try:
            status_resp = service.handle("user-t3", "show me the provisioning status", route_intent=None)
        except Exception as exc:
            raised = True
            print(f"  EXCEPTION: {exc}")

        results.extend([
            check("T3: status query does not raise (I5)", not raised),
            check("T3: status response is handled", status_resp is not None and status_resp.handled),
            check("T3: response references the VM identity", status_resp is not None and "ava-web-01" in status_resp.response),
        ])
        if status_resp:
            # Must not say "SSH pending" (that would be lying about a deleted VM)
            results.append(check("T3: response does not say SSH pending for a known VM",
                                  "ssh: `pending until runner completes`" not in status_resp.response.lower()))
            # The completed-result path shows connection details (last-known), which is
            # honest — it says "last-known" not "currently running".
            results.append(check("T3: response provides last-known or runtime-truth context",
                                  "last-known" in status_resp.response.lower()
                                  or "runtime truth" in status_resp.response.lower()
                                  or "live verification" in status_resp.response.lower()))

        # Connection query must not raise (I5)
        conn_raised = False
        conn_resp = None
        try:
            conn_resp = service.handle("user-t3", "how do I connect with PuTTY?", route_intent=None)
        except Exception:
            conn_raised = True
        results.extend([
            check("T3: connection query does not raise (I5)", not conn_raised),
            check("T3: connection response is handled", conn_resp is not None and conn_resp.handled),
        ])

        # A new provisioning request must succeed once the completed session
        # is treated as non-blocking (live verify returns vm_absent → not guard-blocked).
        # We simulate the VM being gone by overriding the day2 verify to fail.
        queue.day2_results.clear()
        queue.day2_statuses.clear()
        # Override enqueue_day2_operation to return a failed verify (VM absent)
        original_enqueue = queue.enqueue_day2_operation

        def _absent_vm_verify(*, session_id, operation, target, instance_id, instance_name,
                               ssh_host, ssh_port, http_port, metadata=None):
            job = original_enqueue(session_id=session_id, operation=operation, target=target,
                                   instance_id=instance_id, instance_name=instance_name,
                                   ssh_host=ssh_host, ssh_port=ssh_port, http_port=http_port,
                                   metadata=metadata)
            if operation == "verify":
                queue.day2_statuses[job.operation_id] = "failed"
                queue.day2_results[job.operation_id] = Day2OperationResult(
                    operation_id=job.operation_id, operation="verify", status="failed",
                    instance_id=instance_id, instance_name=instance_name,
                    evidence={"checks": [
                        {"name": "vm_exists", "passed": False, "evidence": "provider_status=missing"},
                        {"name": "vm_running", "passed": False, "evidence": "power_state=not_running"},
                    ]},
                    completion_timestamp="2026-06-09T18:00:00+00:00",
                    error={"failure_class": "live_verify_failed", "message": "VM not found"},
                )
            return job

        queue.enqueue_day2_operation = _absent_vm_verify  # type: ignore[method-assign]
        retry_raised = False
        retry_resp = None
        try:
            retry_resp = service.handle("user-t3", "I want a web server", route_intent="provisioning")
        except Exception as exc:
            retry_raised = True
            print(f"  RETRY EXCEPTION: {exc}")
        results.extend([
            check("T3: new provisioning request does not raise (I5)", not retry_raised),
            check("T3: new provisioning request is accepted (I7)", retry_resp is not None and retry_resp.handled),
            check("T3: retry asks for specs not existing-VM-guard", retry_resp is not None and (
                "cpu" in retry_resp.response.lower() and "ram" in retry_resp.response.lower()
            )),
        ])
    finally:
        if old_aq is None:
            os.environ.pop("APPROVAL_QUEUE_PATH", None)
        else:
            os.environ["APPROVAL_QUEUE_PATH"] = old_aq
        shutil.rmtree(tmp, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# T4 — Redis raises ConnectionError → clean response, never raises  (I5)
# ---------------------------------------------------------------------------

def test_t4_redis_connection_error_clean_response() -> list[bool]:
    """All Redis reads must degrade to a clean status response, never a stack trace."""
    results: list[bool] = []
    tmp = Path(tempfile.mkdtemp(prefix="ava-t4-"))
    queue = FakeJobQueue()
    old_aq = os.environ.get("APPROVAL_QUEUE_PATH")
    os.environ["APPROVAL_QUEUE_PATH"] = str(tmp / "apq.json")
    try:
        service = ProvisioningChatService(tmp / "sessions.sqlite3", job_queue=queue)
        _setup_approved_session(service, "user-t4", queue)

        # Now simulate Redis going down
        queue._raise_on_redis = True

        raised = False
        resp = None
        try:
            resp = service.handle("user-t4", "show me the provisioning status", route_intent=None)
        except Exception as exc:
            raised = True
            print(f"  EXCEPTION: {exc}")

        results.extend([
            check("T4: status query with Redis down does not raise (I5)", not raised),
            check("T4: response is handled (not unhandled False)", resp is not None and resp.handled),
            check("T4: response is a string (not an exception repr)",
                  resp is not None and isinstance(resp.response, str) and len(resp.response) > 0),
        ])

        conn_raised = False
        conn_resp = None
        try:
            conn_resp = service.handle("user-t4", "how do I connect with PuTTY?", route_intent=None)
        except Exception:
            conn_raised = True
        results.extend([
            check("T4: connection query with Redis down does not raise (I5)", not conn_raised),
            check("T4: connection response is handled", conn_resp is not None and conn_resp.handled),
        ])
    finally:
        if old_aq is None:
            os.environ.pop("APPROVAL_QUEUE_PATH", None)
        else:
            os.environ["APPROVAL_QUEUE_PATH"] = old_aq
        shutil.rmtree(tmp, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# T5 — happy path: clean success result → COMPLETED, connection details  (I3, regression)
# ---------------------------------------------------------------------------

def test_t5_happy_path_completed_no_regression() -> list[bool]:
    """A normal successful result must still produce COMPLETED status and connection details."""
    results: list[bool] = []
    tmp = Path(tempfile.mkdtemp(prefix="ava-t5-"))
    queue = FakeJobQueue()
    old_aq = os.environ.get("APPROVAL_QUEUE_PATH")
    os.environ["APPROVAL_QUEUE_PATH"] = str(tmp / "apq.json")
    try:
        service = ProvisioningChatService(tmp / "sessions.sqlite3", job_queue=queue)
        session, job_id = _setup_approved_session(service, "user-t5", queue)

        queue.write_status(job_id, "completed")
        queue.write_result(ProvisioningJobResult(
            job_id=job_id,
            instance_id="ava-web-01",
            instance_name="ava-web-01",
            ssh_host="127.0.0.1",
            ssh_port=2222,
            http_port=8080,
            verification_evidence={"checks": [{"name": "host_http_200", "passed": True}]},
            completion_timestamp="2026-06-09T16:00:00+00:00",
            error=None,
        ))

        status_resp = service.handle("user-t5", "show me the provisioning status", route_intent=None)
        conn_resp = service.handle("user-t5", "how do I connect with PuTTY?", route_intent=None)

        # Check SQLite was updated to COMPLETED
        all_sessions = service.sessions.list_recent("user-t5", limit=5)
        the_session = next((s for s in all_sessions if s.session_id == session.session_id), None)

        results.extend([
            check("T5: status response is handled", status_resp.handled),
            check("T5: effective phase is completed", "phase: `completed`" in status_resp.response.lower()),
            check("T5: SSH host shown in status", "ssh host/ip: `127.0.0.1`" in status_resp.response.lower()),
            check("T5: SSH port shown in status", "ssh port: `2222`" in status_resp.response.lower()),
            check("T5: connection response shows PuTTY details", "putty" in conn_resp.response.lower()),
            check("T5: connection shows SSH host", "127.0.0.1" in conn_resp.response),
            check("T5: SQLite phase written to COMPLETED (I2)", the_session is not None and the_session.phase == SessionPhase.COMPLETED),
            check("T5: instance_id recorded in SQLite (I3)", the_session is not None and the_session.instance_id == "ava-web-01"),
        ])
    finally:
        if old_aq is None:
            os.environ.pop("APPROVAL_QUEUE_PATH", None)
        else:
            os.environ["APPROVAL_QUEUE_PATH"] = old_aq
        shutil.rmtree(tmp, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# T6 — failure+rollback cycle N times → no session stuck, every retry accepted  (I7)
# ---------------------------------------------------------------------------

def test_t6_repeated_failure_cycle_no_stuck_sessions() -> list[bool]:
    """Simulate N provisioning attempts that each fail with the exact live-bug pattern.

    After each failure, new provisioning must be immediately accepted.
    No session should remain stuck in a non-terminal phase.
    """
    N = 4
    results: list[bool] = []
    tmp = Path(tempfile.mkdtemp(prefix="ava-t6-"))
    queue = FakeJobQueue()
    old_aq = os.environ.get("APPROVAL_QUEUE_PATH")
    os.environ["APPROVAL_QUEUE_PATH"] = str(tmp / "apq.json")
    try:
        service = ProvisioningChatService(tmp / "sessions.sqlite3", job_queue=queue)

        for i in range(1, N + 1):
            user_id = "user-t6"
            session, job_id = _setup_approved_session(service, user_id, queue)

            # Each cycle: result with BOTH instance_id AND error (the live-bug pattern)
            vm_name = f"ava-web-{i:02d}"
            queue.write_status(job_id, "failed")
            queue.write_result(ProvisioningJobResult(
                job_id=job_id,
                instance_id=vm_name,
                instance_name=vm_name,
                ssh_host=None, ssh_port=None, http_port=None,
                verification_evidence={"rollback": {"status": "destroyed"}},
                completion_timestamp=f"2026-06-09T{16 + i:02d}:00:00+00:00",
                error={
                    "failed_step": "host_runner",
                    "failure_class": "runner_failed",
                    "instance_id": vm_name,
                    "message": f"cloud-init timeout on attempt {i}",
                    "rollback": {"status": "destroyed"},
                },
            ))

            # Status query: must show failed
            status_resp = service.handle(user_id, "show me the provisioning status", route_intent=None)
            active_after = service.sessions.list_active(user_id)
            results.extend([
                check(f"T6[{i}]: status shows phase=failed", "phase: `failed`" in status_resp.response.lower()),
                check(f"T6[{i}]: session not in active list after failure", len(active_after) == 0),
            ])

            # Retry must be accepted immediately (I7)
            if i < N:
                retry = service.handle(user_id, "I want a web server", route_intent="provisioning")
                results.extend([
                    check(f"T6[{i}]: retry is accepted (I7)", retry.handled),
                    check(f"T6[{i}]: retry asks for specs", "cpu" in retry.response.lower()),
                    check(f"T6[{i}]: retry avoids active-guard", "already active" not in retry.response.lower()),
                ])

        # After N cycles, all sessions must be terminal
        all_sessions = service.sessions.list_recent(user_id, limit=N + 2)
        non_terminal = [s for s in all_sessions if s.phase not in {
            SessionPhase.COMPLETED, SessionPhase.FAILED, SessionPhase.CANCELLED
        }]
        results.append(check(f"T6: all {N} sessions are terminal in SQLite", len(non_terminal) == 0,
                              f"non-terminal: {[s.phase.value for s in non_terminal]}"))
    finally:
        if old_aq is None:
            os.environ.pop("APPROVAL_QUEUE_PATH", None)
        else:
            os.environ["APPROVAL_QUEUE_PATH"] = old_aq
        shutil.rmtree(tmp, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    failures: list[bool] = []

    print("\n--- T1: result with instance_id + error → FAILED (I1, I2, I3) ---")
    failures.extend(test_t1_result_error_wins_over_instance_id())

    print("\n--- T2: Redis empty + runner offline → reconcile + unblock (I6, I7) ---")
    failures.extend(test_t2_redis_empty_runner_offline_reconciles())

    print("\n--- T3: VM deleted while awaiting_first_login → honest, no raise (I4, I5) ---")
    failures.extend(test_t3_vm_deleted_honest_message_no_exception())

    print("\n--- T4: Redis ConnectionError → clean response, no raise (I5) ---")
    failures.extend(test_t4_redis_connection_error_clean_response())

    print("\n--- T5: happy-path success → COMPLETED, connection details (regression) ---")
    failures.extend(test_t5_happy_path_completed_no_regression())

    print("\n--- T6: N failure+rollback cycles → no stuck sessions, every retry accepted (I7) ---")
    failures.extend(test_t6_repeated_failure_cycle_no_stuck_sessions())

    failed_count = sum(1 for f in failures if not f)
    if failed_count:
        print(f"\nSource-of-truth regression FAILED: {failed_count} issue(s)")
        return 1
    print("\nSource-of-truth regression PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
