#!/usr/bin/env python3
"""Regression checks for Phase 6 guided provisioning serving integration."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control import approval  # noqa: E402
from control.input_router import route_query  # noqa: E402
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


def session_from_result(service: ProvisioningChatService, result):
    session_id = result.metadata.get("provisioning", {}).get("session_id")
    return service.sessions.get(session_id) if session_id else None


class FakeProvisioningJobQueue:
    def __init__(self, *, live_verify_status: str = "completed", runner_heartbeat=None):
        self.jobs: dict[str, ProvisioningJob] = {}
        self.statuses: dict[str, str] = {}
        self.progress: dict[str, ProvisioningJobProgress] = {}
        self.results: dict[str, ProvisioningJobResult] = {}
        self.day2_jobs: dict[str, Day2OperationJob] = {}
        self.day2_statuses: dict[str, str] = {}
        self.day2_results: dict[str, Day2OperationResult] = {}
        self.counter = 0
        self.day2_counter = 0
        self.runner_healthy = True
        self.live_verify_status = live_verify_status
        self.runner_heartbeat = runner_heartbeat

    def enqueue_approved_job(self, *, session_id, desired_state, credential_id, username, temporary_password):
        self.counter += 1
        job_id = f"job-{self.counter:04d}"
        job = ProvisioningJob(
            job_id=job_id,
            session_id=session_id,
            desired_state=dict(desired_state),
            credentials_seed_data={
                "credential_id": credential_id,
                "username": username,
                "temporary_password": temporary_password,
            },
            enqueued_at="2026-05-02T00:00:00+00:00",
            expires_at="2026-05-02T00:30:00+00:00",
        )
        self.jobs[job_id] = job
        self.statuses[job_id] = "queued"
        return job

    def claim_next_job(self, *, timeout_seconds=30):
        return None

    def get_status(self, job_id):
        return self.statuses.get(job_id)

    def write_status(self, job_id, status):
        self.statuses[job_id] = status

    def get_result(self, job_id):
        return self.results.get(job_id)

    def get_progress(self, job_id):
        return self.progress.get(job_id)

    def write_progress(self, progress):
        self.progress[progress.job_id] = progress

    def write_result(self, result):
        self.results[result.job_id] = result

    def is_runner_healthy(self):
        return self.runner_healthy

    def get_runner_heartbeat(self):
        return self.runner_heartbeat

    def enqueue_day2_operation(
        self,
        *,
        session_id,
        operation,
        target,
        instance_id,
        instance_name,
        ssh_host,
        ssh_port,
        http_port,
        metadata=None,
    ):
        self.day2_counter += 1
        operation_id = f"day2-{self.day2_counter:04d}"
        job = Day2OperationJob(
            operation_id=operation_id,
            session_id=session_id,
            operation=operation,
            target=target,
            instance_id=instance_id,
            instance_name=instance_name,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            http_port=http_port,
            requested_at="2026-05-02T00:15:00+00:00",
            metadata=dict(metadata or {}),
        )
        self.day2_jobs[operation_id] = job
        self.day2_statuses[operation_id] = "queued"
        if operation == "verify":
            if self.live_verify_status == "pending":
                return job
            if self.live_verify_status == "failed":
                self.day2_statuses[operation_id] = "failed"
                self.day2_results[operation_id] = Day2OperationResult(
                    operation_id=operation_id,
                    operation="verify",
                    status="failed",
                    instance_id=instance_id,
                    instance_name=instance_name,
                    evidence={
                        "action": "live_web_verify",
                        "checks": [
                            {"name": "vm_exists", "passed": False, "evidence": "provider_status=missing"},
                            {"name": "vm_running", "passed": False, "evidence": "power_state=not_running"},
                            {"name": "host_http_200", "passed": False, "evidence": f"http://127.0.0.1:{http_port}/ unreachable"},
                        ],
                        "http_port": http_port,
                        "ssh_host": ssh_host,
                        "ssh_port": ssh_port,
                    },
                    completion_timestamp="2026-05-02T00:15:30+00:00",
                    error={"failure_class": "live_verify_failed", "message": "VM is not running or no longer exists"},
                )
                return job
            self.day2_statuses[operation_id] = "completed"
            self.day2_results[operation_id] = Day2OperationResult(
                operation_id=operation_id,
                operation="verify",
                status="completed",
                instance_id=instance_id,
                instance_name=instance_name,
                evidence={
                    "action": "live_web_verify",
                    "checks": [
                        {"name": "vm_exists", "passed": True, "evidence": "provider_status=running"},
                        {"name": "vm_running", "passed": True, "evidence": "power_state=running"},
                        {"name": "host_http_200", "passed": True, "evidence": f"http://127.0.0.1:{http_port}/ -> HTTP 200"},
                    ],
                    "http_port": http_port,
                    "ssh_host": ssh_host,
                    "ssh_port": ssh_port,
                },
                completion_timestamp="2026-05-02T00:15:30+00:00",
            )
        if operation == "nginx_logs":
            self.day2_statuses[operation_id] = "completed"
            self.day2_results[operation_id] = Day2OperationResult(
                operation_id=operation_id,
                operation="nginx_logs",
                status="completed",
                instance_id=instance_id,
                instance_name=instance_name,
                evidence={
                    "action": "nginx_logs",
                    "journalctl_tail": "nginx.service active",
                    "access_log_tail": "GET / HTTP/1.1 200",
                    "error_log_tail": "",
                    "ssh_host": ssh_host,
                    "ssh_port": ssh_port,
                },
                completion_timestamp="2026-05-02T00:15:45+00:00",
            )
        if operation == "open_ssh_console":
            self.day2_statuses[operation_id] = "completed"
            self.day2_results[operation_id] = Day2OperationResult(
                operation_id=operation_id,
                operation="open_ssh_console",
                status="completed",
                instance_id=instance_id,
                instance_name=instance_name,
                evidence={
                    "action": "ssh_console_launched",
                    "tool": "PuTTY",
                    "ssh_host": ssh_host,
                    "ssh_port": ssh_port,
                    "username": "avaadmin",
                    "password_handling": "not_passed_to_process",
                },
                completion_timestamp="2026-05-02T00:15:50+00:00",
            )
        return job

    def claim_next_day2_operation(self, *, timeout_seconds=1):
        return None

    def get_day2_status(self, operation_id):
        return self.day2_statuses.get(operation_id)

    def write_day2_status(self, operation_id, status):
        self.day2_statuses[operation_id] = status

    def get_day2_result(self, operation_id):
        return self.day2_results.get(operation_id)

    def write_day2_result(self, result):
        self.day2_results[result.operation_id] = result


def main() -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="ava-phase6-serving-"))
    old_queue = os.environ.get("APPROVAL_QUEUE_PATH")
    os.environ["APPROVAL_QUEUE_PATH"] = str(temp_dir / "approval_queue.json")
    try:
        job_queue = FakeProvisioningJobQueue()
        service = ProvisioningChatService(temp_dir / "provisioning_sessions.sqlite3", job_queue=job_queue)
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

        start_named = service.handle("user-named", "I want a web server", route_intent="provisioning")
        named_specs = service.handle(
            "user-named",
            "2 CPU, 4 GB RAM, 30 GB disk, hostname ava-web-01",
            route_intent=None,
        )
        named_session = service.sessions.list_active("user-named")[0]
        failures.extend(
            [
                check("named host start is handled", start_named.handled),
                check("hostname spec answer is handled", named_specs.handled),
                check("hostname is recorded in desired state", named_session.desired_state.get("vm_name") == "ava-web-01"),
                check("plan response displays hostname", "hostname: `ava-web-01`" in named_specs.response.lower()),
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

        direct_routing_service = ProvisioningChatService(
            temp_dir / "direct_routing_sessions.sqlite3",
            job_queue=FakeProvisioningJobQueue(),
        )
        direct_additional_start = direct_routing_service.handle("user-direct", "create another web server", route_intent=None)
        failures.extend(
            [
                check("serving contract catches explicit additional web server without router", direct_additional_start.handled),
                check(
                    "serving contract additional request asks for specs",
                    "cpu" in direct_additional_start.response.lower() and "ram" in direct_additional_start.response.lower(),
                ),
                check(
                    "serving contract additional request avoids generic boundary",
                    "focused on devops" not in direct_additional_start.response.lower(),
                ),
            ]
        )

        duplicate_queue = FakeProvisioningJobQueue()
        duplicate_service = ProvisioningChatService(
            temp_dir / "duplicate_name_sessions.sqlite3",
            job_queue=duplicate_queue,
        )
        duplicate_service.handle("user-duplicate", "I want a web server", route_intent="provisioning")
        duplicate_specs = duplicate_service.handle(
            "user-duplicate",
            "2 CPU, 4 GB RAM, 30 GB disk, hostname ava-web-known",
            route_intent=None,
        )
        duplicate_approval_id = duplicate_specs.metadata["provisioning"]["approval_id"]
        duplicate_service.handle("user-duplicate", f"approve {duplicate_approval_id}", route_intent=None)
        duplicate_service.handle("user-duplicate", "continue provisioning", route_intent=None)
        duplicate_queue.write_status("job-0001", "completed")
        duplicate_queue.write_result(
            ProvisioningJobResult(
                job_id="job-0001",
                instance_id="ava-web-known",
                instance_name="ava-web-known",
                ssh_host="127.0.0.1",
                ssh_port=2222,
                http_port=8080,
                verification_evidence={"checks": [{"name": "host_http_200", "passed": True, "evidence": "HTTP 200"}]},
                completion_timestamp="2026-05-02T00:12:00+00:00",
                error=None,
            )
        )
        duplicate_start = duplicate_service.handle("user-duplicate", "create another web server", route_intent=None)
        duplicate_conflict = duplicate_service.handle(
            "user-duplicate",
            "2 CPU, 4 GB RAM, 30 GB disk, hostname ava-web-known",
            route_intent=None,
        )
        duplicate_conflict_session = session_from_result(duplicate_service, duplicate_conflict)
        duplicate_retry = duplicate_service.handle("user-duplicate", "I want another web server", route_intent=None)
        failures.extend(
            [
                check("explicit second server bypasses existing VM guard", "already have an ava-managed web server" not in duplicate_start.response.lower()),
                check("explicit second server asks for specs", "cpu" in duplicate_start.response.lower() and "ram" in duplicate_start.response.lower()),
                check("known duplicate hostname is blocked before approval", duplicate_conflict.handled),
                check("known duplicate hostname names the conflict", "cannot use hostname `ava-web-known`" in duplicate_conflict.response.lower()),
                check("known duplicate hostname does not create approval", "no approval was created" in duplicate_conflict.response.lower()),
                check(
                    "known duplicate hostname marks conflict session failed",
                    duplicate_conflict_session and duplicate_conflict_session.phase == SessionPhase.FAILED,
                    duplicate_conflict_session.phase.value if duplicate_conflict_session else "missing session",
                ),
                check(
                    "known duplicate hostname allows immediate retry",
                    "already active" not in duplicate_retry.response.lower()
                    and "cpu" in duplicate_retry.response.lower()
                    and "ram" in duplicate_retry.response.lower(),
                ),
            ]
        )

        infra_queue = FakeProvisioningJobQueue(
            runner_heartbeat={"status": "idle", "metadata": {"registered_vms": ["ava-web-live"]}}
        )
        infra_service = ProvisioningChatService(
            temp_dir / "infra_duplicate_sessions.sqlite3",
            job_queue=infra_queue,
        )
        infra_service.handle("user-infra", "I want a web server", route_intent="provisioning")
        infra_conflict = infra_service.handle(
            "user-infra",
            "2 CPU, 4 GB RAM, 30 GB disk, hostname ava-web-live",
            route_intent=None,
        )
        infra_conflict_session = session_from_result(infra_service, infra_conflict)
        infra_retry = infra_service.handle("user-infra", "I want a web server", route_intent=None)
        failures.extend(
            [
                check("live VirtualBox duplicate hostname is blocked before approval", infra_conflict.handled),
                check(
                    "live VirtualBox duplicate hostname explains infrastructure conflict",
                    "already exists in the local infrastructure" in infra_conflict.response.lower(),
                ),
                check("live VirtualBox duplicate hostname does not create approval", "no approval was created" in infra_conflict.response.lower()),
                check("live VirtualBox duplicate hostname does not queue job", len(infra_queue.jobs) == 0),
                check(
                    "live VirtualBox duplicate hostname marks conflict session failed",
                    infra_conflict_session and infra_conflict_session.phase == SessionPhase.FAILED,
                    infra_conflict_session.phase.value if infra_conflict_session else "missing session",
                ),
                check(
                    "live VirtualBox duplicate hostname allows immediate retry",
                    "already active" not in infra_retry.response.lower()
                    and "cpu" in infra_retry.response.lower()
                    and "ram" in infra_retry.response.lower(),
                ),
            ]
        )

        external_vm_name = f"external-vm-{uuid.uuid4().hex[:8]}"
        inventory_queue = FakeProvisioningJobQueue(
            runner_heartbeat={
                "status": "idle",
                "updated_at": "2026-05-02T00:20:00+00:00",
                "metadata": {
                    "registered_vm_inventory": [
                        {
                            "name": external_vm_name,
                            "exists": True,
                            "power_state": "poweroff",
                            "provider_status": "poweroff",
                        }
                    ]
                },
            }
        )
        inventory_service = ProvisioningChatService(
            temp_dir / "external_inventory_sessions.sqlite3",
            job_queue=inventory_queue,
        )
        external_inventory = inventory_service.handle("user-inventory", "list my servers", route_intent=None)
        inventory_service.handle("user-inventory", "I want a web server", route_intent="provisioning")
        external_conflict = inventory_service.handle(
            "user-inventory",
            f"2 CPU, 4 GB RAM, 30 GB disk, hostname {external_vm_name}",
            route_intent=None,
        )
        external_conflict_session = session_from_result(inventory_service, external_conflict)
        failures.extend(
            [
                check("live inventory query lists VirtualBox-only VM", external_vm_name in external_inventory.response),
                check(
                    "live inventory marks external VM as not AVA-managed",
                    "visible in virtualbox only" in external_inventory.response.lower(),
                ),
                check("live inventory reports external VM power state", "power state: `poweroff`" in external_inventory.response.lower()),
                check("external VirtualBox duplicate hostname is blocked before approval", external_conflict.handled),
                check(
                    "external VirtualBox duplicate hostname explains infrastructure conflict",
                    "already exists in the local infrastructure" in external_conflict.response.lower(),
                ),
                check("external VirtualBox duplicate hostname does not create approval", "no approval was created" in external_conflict.response.lower()),
                check("external VirtualBox duplicate hostname does not queue job", len(inventory_queue.jobs) == 0),
                check(
                    "external VirtualBox duplicate hostname marks conflict session failed",
                    external_conflict_session and external_conflict_session.phase == SessionPhase.FAILED,
                    external_conflict_session.phase.value if external_conflict_session else "missing session",
                ),
            ]
        )

        after_queue = FakeProvisioningJobQueue()
        after_service = ProvisioningChatService(
            temp_dir / "after_approval_duplicate_sessions.sqlite3",
            job_queue=after_queue,
        )
        after_service.handle("user-after-conflict", "I want a web server", route_intent="provisioning")
        after_specs = after_service.handle(
            "user-after-conflict",
            "2 CPU, 4 GB RAM, 30 GB disk, hostname ava-web-after-approval",
            route_intent=None,
        )
        after_approval_id = after_specs.metadata["provisioning"]["approval_id"]
        after_service.handle("user-after-conflict", f"approve {after_approval_id}", route_intent=None)
        after_queue.runner_heartbeat = {
            "status": "idle",
            "metadata": {"registered_vms": ["ava-web-after-approval"]},
        }
        after_conflict = after_service.handle("user-after-conflict", "continue provisioning", route_intent=None)
        after_conflict_session = session_from_result(after_service, after_conflict)
        after_retry = after_service.handle("user-after-conflict", "I want a web server", route_intent=None)
        failures.extend(
            [
                check("post-approval VirtualBox duplicate is blocked before runner job", after_conflict.handled),
                check(
                    "post-approval VirtualBox duplicate names infrastructure conflict",
                    "already exists in the local infrastructure" in after_conflict.response.lower(),
                ),
                check("post-approval VirtualBox duplicate avoids temporary credential", "temporary password:" not in after_conflict.response.lower()),
                check("post-approval VirtualBox duplicate does not queue job", len(after_queue.jobs) == 0),
                check(
                    "post-approval VirtualBox duplicate marks conflict session failed",
                    after_conflict_session and after_conflict_session.phase == SessionPhase.FAILED,
                    after_conflict_session.phase.value if after_conflict_session else "missing session",
                ),
                check(
                    "post-approval VirtualBox duplicate allows immediate retry",
                    "already active" not in after_retry.response.lower()
                    and "cpu" in after_retry.response.lower()
                    and "ram" in after_retry.response.lower(),
                ),
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

        offline_queue = FakeProvisioningJobQueue()
        offline_queue.runner_healthy = False
        offline_service = ProvisioningChatService(temp_dir / "offline_sessions.sqlite3", job_queue=offline_queue)
        offline_service.handle("user-offline", "I want a web server in Ubuntu", route_intent="provisioning")
        offline_specs = offline_service.handle("user-offline", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        offline_approval_id = offline_specs.metadata["provisioning"]["approval_id"]
        offline_approval = offline_service.handle("user-offline", f"approve {offline_approval_id}", route_intent=None)
        offline_session = offline_service.sessions.list_active("user-offline")[0]
        failures.extend(
            [
                check("offline runner approval is handled", offline_approval.handled),
                check("offline runner approval records consent", "approval recorded" in offline_approval.response.lower()),
                check("offline runner approval asks to continue later", "continue provisioning" in offline_approval.response.lower()),
                check(
                    "offline runner approval does not print credential block",
                    "username:" not in offline_approval.response.lower()
                    and "temporary password:" not in offline_approval.response.lower(),
                ),
                check("offline runner marks approval approved", approval.get_by_id(offline_approval_id).get("status") == "approved"),
                check("offline runner leaves session awaiting approval", offline_session.phase == SessionPhase.AWAITING_APPROVAL, offline_session.phase.value),
                check("offline runner queue stays empty", len(offline_queue.jobs) == 0),
            ]
        )

        offline_continue_queue = FakeProvisioningJobQueue()
        offline_continue_queue.runner_healthy = False
        offline_continue_service = ProvisioningChatService(
            temp_dir / "offline_continue_sessions.sqlite3",
            job_queue=offline_continue_queue,
        )
        offline_continue_service.handle("user-offline-continue", "I want a web server in Ubuntu", route_intent="provisioning")
        offline_continue_service.handle("user-offline-continue", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        offline_continue = offline_continue_service.handle("user-offline-continue", "continue provisioning", route_intent=None)
        failures.extend(
            [
                check("offline runner continuation is handled", offline_continue.handled),
                check(
                    "offline runner continuation blocks VM queue",
                    "runner is not reporting healthy" in offline_continue.response.lower(),
                ),
                check("offline runner continuation queue stays empty", len(offline_continue_queue.jobs) == 0),
            ]
        )

        approved = service.handle("user-1", f"approve - {approval_id}", route_intent=None)
        approved_session = service.sessions.list_active("user-1")[0]
        failures.extend(
            [
                check("chat approval with separator is handled", approved.handled),
                check("chat approval updates queue", approval.get_by_id(approval_id).get("status") == "approved"),
                check("chat approval records consent only", "approval recorded" in approved.response.lower()),
                check("chat approval asks for explicit continuation", "continue provisioning" in approved.response.lower()),
                check("chat approval does not issue credential yet", "temporary password:" not in approved.response.lower()),
                check("chat approval does not queue runner yet", len(job_queue.jobs) == 0),
                check("approved phase remains awaiting approval until continuation", approved_session.phase == SessionPhase.AWAITING_APPROVAL, approved_session.phase.value),
            ]
        )

        continued = service.handle("user-1", "continue provisioning", route_intent=None)
        continued_session = service.sessions.list_active("user-1")[0]
        failures.extend(
            [
                check("approved continuation is handled", continued.handled),
                check("approved continuation issues one-time credential", "temporary password" in continued.response.lower()),
                check("approved continuation queues runner job", "runner job queued" in continued.response.lower()),
                check("approved continuation clarifies runner boundary", "host-side" in continued.response.lower()),
                check("approved continuation explains putty endpoint later", "putty ssh host/ip" in continued.response.lower()),
                check("runner job contains temporary seed secret", bool(job_queue.jobs["job-0001"].credentials_seed_data.get("temporary_password"))),
                check("continued phase awaits first login", continued_session.phase == SessionPhase.AWAITING_FIRST_LOGIN, continued_session.phase.value),
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

        job_queue.write_status("job-0001", "bootstrapping")
        job_queue.write_progress(
            ProvisioningJobProgress(
                job_id="job-0001",
                session_id=hardening_session.session_id,
                status="bootstrapping",
                stage="web_bootstrap",
                instance_id="ava-web-progress",
                instance_name="ava-web-progress",
                ssh_host="127.0.0.1",
                ssh_port=2222,
                http_port=8080,
                runner_key_path=".ava-runner/keys/job-0001_ava_runner_ed25519",
                message="Installing and enabling the web-server role.",
                updated_at="2026-05-02T00:08:00+00:00",
            )
        )
        progress_status = service.handle("user-1", "show me the provisioning status", route_intent=None)
        progress_connection = service.handle("user-1", "how do I connect with PuTTY?", route_intent=None)
        progress_verify = service.handle("user-1", "verify the web server", route_intent=None)
        progress_session = service.sessions.list_active("user-1")[0]
        failures.extend(
            [
                check("runner progress attaches visible VM", progress_session.instance_id == "ava-web-progress", progress_session.instance_id or ""),
                check("progress status shows attached VM", "ava-web-progress" in progress_status.response),
                check("progress status shows runner progress", "runner progress" in progress_status.response.lower()),
                check("progress status shows SSH details", "ssh host/ip: `127.0.0.1`" in progress_status.response.lower()),
                check("progress connection shows in-progress details", "runner progress details" in progress_connection.response.lower()),
                check("progress connection includes SSH port", "ssh port: `2222`" in progress_connection.response.lower()),
                check(
                    "progress verification is honest",
                    "verification is not complete" in progress_verify.response.lower()
                    or "live verification has been queued" in progress_verify.response.lower()
                    or "live web-server verification" in progress_verify.response.lower(),
                ),
                check("progress verification names visible VM", "ava-web-progress" in progress_verify.response),
            ]
        )

        verify = service.handle("user-1", "verify the web server", route_intent=None)
        status = service.handle("user-1", "show me the provisioning status", route_intent=None)
        evidence = service.handle("user-1", "what did you do and what evidence do you have?", route_intent=None)
        failures.extend(
            [
                check("web verification follow-up stays in provisioning session", verify.handled),
                check(
                    "web verification reports queued runner honestly",
                    "runner status" in verify.response.lower()
                    or "live verification has been queued" in verify.response.lower()
                    or "live web-server verification" in verify.response.lower(),
                ),
                check("status follow-up stays in provisioning session", status.handled),
                check("status reports current phase", "bootstrapping" in status.response.lower()),
                check("evidence follow-up stays in provisioning session", evidence.handled),
                check("evidence reports runner job", "runner job id" in evidence.response.lower()),
            ]
        )

        job_queue.write_status("job-0001", "completed")
        job_queue.write_result(
            ProvisioningJobResult(
                job_id="job-0001",
                instance_id="ava-web-01",
                instance_name="ava-web-01",
                ssh_host="127.0.0.1",
                ssh_port=2222,
                http_port=8080,
                verification_evidence={
                    "checks": [
                        {"name": "host_http_200", "passed": True, "evidence": "HTTP 200"},
                    ]
                },
                completion_timestamp="2026-05-02T00:10:00+00:00",
                error=None,
            )
        )
        completed_status = service.handle("user-1", "show me the provisioning status", route_intent=None)
        completed_verify = service.handle("user-1", "verify the web server", route_intent=None)
        completed_connection = service.handle("user-1", "how do I connect with PuTTY?", route_intent=None)
        completed_evidence = service.handle("user-1", "what did you do and what evidence do you have?", route_intent=None)
        day2_status = service.handle("user-1", "show status of my web server", route_intent=None)
        day2_logs = service.handle("user-1", "show nginx logs", route_intent=None)
        job_queue.runner_heartbeat = {
            "status": "idle",
            "metadata": {
                "registered_vm_inventory": [
                    {
                        "name": "ava-web-01",
                        "exists": True,
                        "power_state": "saved",
                        "provider_status": "saved",
                    }
                ]
            },
        }
        server_inventory = service.handle("user-1", "list my servers", route_intent=None)
        offline_inventory = service.handle("user-1", "show offline servers", route_intent=None)
        unnamed_start = service.handle("user-1", "start server", route_intent=None)
        named_start = service.handle("user-1", "start ava-web-01", route_intent=None)
        named_start_approval_id = named_start.metadata["provisioning"]["approval_id"]
        named_start_approved = service.handle("user-1", f"approve {named_start_approval_id}", route_intent=None)
        named_start_operation_id = named_start_approved.response.split("Operation ID: `", 1)[1].split("`", 1)[0]
        named_verify = service.handle("user-1", "verify ava-web-01", route_intent=None)
        named_logs = service.handle("user-1", "show nginx logs for ava-web-01", route_intent=None)
        named_delete = service.handle("user-1", "delete ava-web-01", route_intent=None)
        named_delete_approval_id = named_delete.metadata["provisioning"]["approval_id"]
        named_delete_approved = service.handle("user-1", f"approve {named_delete_approval_id}", route_intent=None)
        named_delete_operation_id = named_delete_approved.response.split("Operation ID: `", 1)[1].split("`", 1)[0]
        day2_job_count_before_connection_help = len(job_queue.day2_jobs)
        day2_connection_help = service.handle("user-1", "how do I connect with PuTTY?", route_intent=None)
        day2_job_count_after_connection_help = len(job_queue.day2_jobs)
        day2_open_console = service.handle("user-1", "open PuTTY", route_intent=None)
        day2_restart = service.handle("user-1", "restart nginx", route_intent=None)
        day2_approval_id = day2_restart.metadata["provisioning"]["approval_id"]
        day2_approval_entry = approval.get_by_id(day2_approval_id)
        day2_approved = service.handle("user-1", f"approve {day2_approval_id}", route_intent=None)
        day2_operation_id = day2_approved.response.split("Operation ID: `", 1)[1].split("`", 1)[0]
        day2_snapshot = service.handle("user-1", "take a snapshot before changes", route_intent=None)
        day2_job_count_before_snapshot_status = len(job_queue.day2_jobs)
        day2_snapshot_status = service.handle("user-1", "snapshot status", route_intent=None)
        day2_rollback = service.handle("user-1", "rollback to last snapshot", route_intent=None)
        job_queue.statuses.pop("job-0001", None)
        expired_status = service.handle("user-1", "show me the provisionning status", route_intent=None)
        expired_evidence = service.handle("user-1", "what evidence do you have?", route_intent=None)
        issued_secret = job_queue.jobs["job-0001"].credentials_seed_data["temporary_password"]
        failures.extend(
            [
                check("completed status shows putty ssh host", "ssh host/ip: `127.0.0.1`" in completed_status.response.lower()),
                check("completed status shows putty ssh port", "ssh port: `2222`" in completed_status.response.lower()),
                check("completed status reports effective phase", "phase: `completed`" in completed_status.response.lower()),
                check("completed status keeps conversation checkpoint visible", "conversation checkpoint:" in completed_status.response.lower()),
                check("completed verify shows http evidence", "http://127.0.0.1:8080/" in completed_verify.response.lower()),
                check("completed verify uses live runner evidence", "live web-server verification" in completed_verify.response.lower()),
                check("connection query shows putty host", "putty host name" in completed_connection.response.lower()),
                check("connection query shows username", "username: `avaadmin`" in completed_connection.response.lower()),
                check("connection query does not reprint actual password", issued_secret not in completed_connection.response),
                check("evidence includes hardening summary", "hardening summary" in completed_evidence.response.lower()),
                check("evidence reports effective completed phase", "effective phase: `completed`" in completed_evidence.response.lower()),
                check("evidence states apache not applied", "apache hardening" in completed_evidence.response.lower()),
                check("day-2 status is handled", day2_status.handled),
                check("day-2 status reports active vm", "ava-web-01" in day2_status.response),
                check("day-2 logs are handled", day2_logs.handled),
                check("server inventory is handled", server_inventory.handled),
                check("server inventory lists completed VM", "`ava-web-01`" in server_inventory.response),
                check("server inventory shows live power state", "power state: `saved`" in server_inventory.response.lower()),
                check("server inventory shows live provider status", "provider status: `saved`" in server_inventory.response.lower()),
                check("server inventory guides offline server start", "start ava-web-01" in server_inventory.response.lower()),
                check("server inventory guides offline server delete", "delete ava-web-01" in server_inventory.response.lower()),
                check("server inventory shows targeted examples", "open web console for ava-web-03" in server_inventory.response.lower()),
                check("offline inventory query is handled", offline_inventory.handled),
                check("unnamed start asks for exact hostname", "which server should i start" in unnamed_start.response.lower()),
                check("unnamed start does not create approval", "approval required" not in unnamed_start.response.lower()),
                check("named start requires approval", "approval required" in named_start.response.lower()),
                check("named start approval is handled", named_start_approved.handled),
                check("named start queues runner operation", named_start_operation_id in job_queue.day2_jobs),
                check("named start queues start_vm", job_queue.day2_jobs[named_start_operation_id].operation == "start_vm"),
                check("named verify is handled", named_verify.handled),
                check("named verify targets requested VM", "ava-web-01" in named_verify.response),
                check("named nginx logs is handled", named_logs.handled),
                check("named nginx logs targets requested VM", "ava-web-01" in named_logs.response),
                check("named delete requires approval", "approval required" in named_delete.response.lower()),
                check("named delete is high risk", "risk: `high`" in named_delete.response.lower()),
                check("named delete approval is handled", named_delete_approved.handled),
                check("named delete queues runner operation", named_delete_operation_id in job_queue.day2_jobs),
                check("named delete queues delete_vm", job_queue.day2_jobs[named_delete_operation_id].operation == "delete_vm"),
                check("putty help stays informational", "putty settings" in day2_connection_help.response.lower()),
                check("putty help does not queue console launch", day2_job_count_after_connection_help == day2_job_count_before_connection_help),
                check("open putty is handled", day2_open_console.handled),
                check("open putty queues host-runner console launch", "ssh console launch requested" in day2_open_console.response.lower()),
                check("open putty records no password passing", "does not pass or reprint the password" in day2_open_console.response.lower()),
                check("day-2 status hides internal phase name", "day-2" not in day2_status.response.lower() and "phase 9" not in day2_status.response.lower()),
                check("day-2 logs uses live runner evidence", "live nginx logs" in day2_logs.response.lower()),
                check("day-2 logs hides internal phase name", "day-2" not in day2_logs.response.lower() and "phase 9" not in day2_logs.response.lower()),
                check("day-2 restart requires approval", "approval required" in day2_restart.response.lower()),
                check("day-2 restart hides internal phase name", "day-2" not in day2_restart.response.lower() and "phase 9" not in day2_restart.response.lower()),
                check("day-2 approval entry is tagged", (day2_approval_entry or {}).get("metadata", {}).get("type") == "day2_operation"),
                check("day-2 approval records risk", (day2_approval_entry or {}).get("risk") == "medium"),
                check("day-2 approval is handled", day2_approved.handled),
                check("day-2 approval queues runner operation", "execution status: `queued`" in day2_approved.response.lower()),
                check("day-2 approval includes operation id", day2_operation_id in job_queue.day2_jobs),
                check("day-2 approval hides internal phase name", "day-2" not in day2_approved.response.lower() and "phase 9" not in day2_approved.response.lower()),
                check("day-2 snapshot requires approval", "operation: `snapshot`" in day2_snapshot.response.lower()),
                check("snapshot status is handled as read-only status", day2_snapshot_status.handled),
                check("snapshot status does not create new approval", "approval required" not in day2_snapshot_status.response.lower()),
                check("snapshot status does not queue another operation", len(job_queue.day2_jobs) == day2_job_count_before_snapshot_status),
                check("day-2 rollback is high risk", "risk: `high`" in day2_rollback.response.lower()),
                check("misspelled status query stays in provisioning session", expired_status.handled),
                check("expired runner status derives completed from result", "runner status: `completed`" in expired_status.response.lower()),
                check("expired runner status still shows effective completed phase", "phase: `completed`" in expired_status.response.lower()),
                check("expired runner status still shows putty details", "ssh port: `2222`" in expired_status.response.lower()),
                check("expired runner evidence derives completed from result", "runner status: `completed`" in expired_evidence.response.lower()),
            ]
        )
        job_queue.write_day2_status(day2_operation_id, "completed")
        job_queue.write_day2_result(
            Day2OperationResult(
                operation_id=day2_operation_id,
                operation="restart_nginx",
                status="completed",
                instance_id="ava-web-01",
                instance_name="ava-web-01",
                evidence={"action": "service_restarted"},
                completion_timestamp="2026-05-02T00:16:00+00:00",
                error=None,
            )
        )
        day2_completed_status = service.handle("user-1", "show status of my web server", route_intent=None)
        failures.extend(
            [
                check("day-2 completed operation appears in status", "latest server-management operation" in day2_completed_status.response.lower()),
                check("day-2 completed operation shows completed", "status: `completed`" in day2_completed_status.response.lower()),
            ]
        )
        service.sessions.update_phase(approved_session.session_id, SessionPhase.BOOTSTRAPPING)
        completed_late_login = service.handle("user-1", "I logged in and changed the password", route_intent=None)
        completed_late_hardening = service.handle("user-1", "yes harden it", route_intent=None)
        failures.extend(
            [
                check("completed bootstrapping login follow-up is handled", completed_late_login.handled),
                check("completed bootstrapping login does not fall through to scope response", "v1.0.3 is scoped" not in completed_late_login.response.lower()),
                check("completed bootstrapping login says no extra step is waiting", "no extra provisioning step" in completed_late_login.response.lower()),
                check("completed bootstrapping hardening follow-up is handled", completed_late_hardening.handled),
                check("completed bootstrapping hardening does not fall through to scope response", "v1.0.3 is scoped" not in completed_late_hardening.response.lower()),
                check("completed bootstrapping hardening says already done", "already done" in completed_late_hardening.response.lower()),
                check("completed bootstrapping hardening includes baseline summary", "baseline_linux" in completed_late_hardening.response.lower()),
            ]
        )

        late_start = service.handle("user-late", "I want a web server in Ubuntu", route_intent="provisioning")
        late_specs = service.handle("user-late", "2 CPU, 4 GB RAM, 30 GB disk, hostname ava-web-late", route_intent=None)
        late_approval_id = late_specs.metadata["provisioning"]["approval_id"]
        late_approved = service.handle("user-late", f"approve {late_approval_id}", route_intent=None)
        late_continued = service.handle("user-late", "continue provisioning", route_intent=None)
        job_queue.write_status("job-0002", "completed")
        job_queue.write_result(
            ProvisioningJobResult(
                job_id="job-0002",
                instance_id="ava-web-late",
                instance_name="ava-web-late",
                ssh_host="127.0.0.1",
                ssh_port=2223,
                http_port=8081,
                verification_evidence={"checks": [{"name": "host_http_200", "passed": True, "evidence": "HTTP 200"}]},
                completion_timestamp="2026-05-02T00:20:00+00:00",
                error=None,
            )
        )
        late_login = service.handle("user-late", "I logged in and changed the password", route_intent=None)
        late_hardening = service.handle("user-late", "yes harden it", route_intent=None)
        failures.extend(
            [
                check("late completed start is handled", late_start.handled),
                check("late completed approval is handled", late_approved.handled),
                check("late completed continuation is handled", late_continued.handled),
                check("late login does not ask for hardening again", "reply `yes harden it`" not in late_login.response.lower()),
                check("late login reports runner already completed", "already completed" in late_login.response.lower()),
                check("late hardening does not claim next execution step", "next execution step" not in late_hardening.response.lower()),
                check("late hardening reports runner-applied baseline", "already applied" in late_hardening.response.lower()),
            ]
        )

        service.sessions.update_phase(approved_session.session_id, SessionPhase.COMPLETED)
        recent_completed_login = service.handle("user-1", "I logged in and changed the password", route_intent=None)
        recent_completed_hardening = service.handle("user-1", "yes harden it", route_intent=None)
        recent_completed_verify = service.handle("user-1", "verify the web server", route_intent=None)
        failures.extend(
            [
                check("recent completed login follow-up is handled", recent_completed_login.handled),
                check("recent completed login follow-up does not fall through", "v1.0.3 is scoped" not in recent_completed_login.response.lower()),
                check("recent completed login points to existing VM", "ava-web-01" in recent_completed_login.response),
                check("recent completed hardening follow-up is handled", recent_completed_hardening.handled),
                check("recent completed hardening does not fall through", "v1.0.3 is scoped" not in recent_completed_hardening.response.lower()),
                check("recent completed verify still works", recent_completed_verify.handled and "http://127.0.0.1:8080/" in recent_completed_verify.response.lower()),
            ]
        )
        accidental_second_server = service.handle("user-1", "I want a web server", route_intent="provisioning")
        explicit_second_server = service.handle("user-1", "create another web server", route_intent="provisioning")
        failures.extend(
            [
                check("existing managed VM blocks accidental second server", accidental_second_server.handled),
                check("existing managed VM guard names current VM", "ava-web-01" in accidental_second_server.response),
                check("existing managed VM guard avoids duplicate creation", "will not create a second one by accident" in accidental_second_server.response.lower()),
                check("explicit second server request is allowed", explicit_second_server.handled),
                check("explicit second server asks for specs", "cpu" in explicit_second_server.response.lower() and "ram" in explicit_second_server.response.lower()),
            ]
        )

        missing_live_queue = FakeProvisioningJobQueue(live_verify_status="failed")
        missing_live_service = ProvisioningChatService(temp_dir / "missing_live_sessions.sqlite3", job_queue=missing_live_queue)
        missing_live_service.handle("user-missing-live", "I want a web server", route_intent="provisioning")
        missing_live_specs = missing_live_service.handle("user-missing-live", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        missing_live_approval_id = missing_live_specs.metadata["provisioning"]["approval_id"]
        missing_live_service.handle("user-missing-live", f"approve {missing_live_approval_id}", route_intent=None)
        missing_live_service.handle("user-missing-live", "continue provisioning", route_intent=None)
        missing_live_queue.write_status("job-0001", "completed")
        missing_live_queue.write_result(
            ProvisioningJobResult(
                job_id="job-0001",
                instance_id="ava-web-deleted",
                instance_name="ava-web-deleted",
                ssh_host="127.0.0.1",
                ssh_port=2222,
                http_port=8080,
                verification_evidence={"checks": [{"name": "host_http_200", "passed": True, "evidence": "old HTTP 200"}]},
                completion_timestamp="2026-05-02T00:25:00+00:00",
                error=None,
            )
        )
        missing_live_verify = missing_live_service.handle("user-missing-live", "verify the web server", route_intent=None)
        missing_live_status = missing_live_service.handle("user-missing-live", "show status of my web server", route_intent=None)
        missing_live_retry = missing_live_service.handle("user-missing-live", "I want a web server", route_intent="provisioning")
        failures.extend(
            [
                check("missing live VM verify is handled", missing_live_verify.handled),
                check("missing live VM verify reports live failure", "could not confirm the server" in missing_live_verify.response.lower()),
                check("missing live VM verify does not recycle stored success", "old http 200" not in missing_live_verify.response.lower()),
                check("missing live VM verify shows failed vm_exists", "vm_exists: `failed`" in missing_live_verify.response.lower()),
                check("missing live VM status labels stored evidence", "stored provisioning result" in missing_live_status.response.lower()),
                check("missing live VM status shows failed live verification", "latest live verification: `failed`" in missing_live_status.response.lower()),
                check("missing live VM no longer blocks new request", missing_live_retry.handled),
                check("missing live VM retry asks for specs", "cpu" in missing_live_retry.response.lower() and "ram" in missing_live_retry.response.lower()),
                check("missing live VM retry avoids active guard", "already active" not in missing_live_retry.response.lower()),
            ]
        )

        offline_existing_queue = FakeProvisioningJobQueue()
        offline_existing_queue.runner_healthy = False
        offline_existing_service = ProvisioningChatService(temp_dir / "offline_existing_sessions.sqlite3", job_queue=offline_existing_queue)
        offline_existing_service.handle("user-offline-existing", "I want a web server", route_intent="provisioning")
        offline_existing_specs = offline_existing_service.handle("user-offline-existing", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        offline_existing_approval_id = offline_existing_specs.metadata["provisioning"]["approval_id"]
        offline_existing_queue.runner_healthy = True
        offline_existing_service.handle("user-offline-existing", f"approve {offline_existing_approval_id}", route_intent=None)
        offline_existing_service.handle("user-offline-existing", "continue provisioning", route_intent=None)
        offline_existing_queue.write_status("job-0001", "completed")
        offline_existing_queue.write_result(
            ProvisioningJobResult(
                job_id="job-0001",
                instance_id="ava-web-last-known",
                instance_name="ava-web-last-known",
                ssh_host="127.0.0.1",
                ssh_port=2222,
                http_port=8080,
                verification_evidence={"checks": [{"name": "host_http_200", "passed": True, "evidence": "old HTTP 200"}]},
                completion_timestamp="2026-05-02T00:27:00+00:00",
                error=None,
            )
        )
        offline_existing_queue.runner_healthy = False
        offline_existing_retry = offline_existing_service.handle("user-offline-existing", "I want a web server", route_intent="provisioning")
        failures.extend(
            [
                check("offline live truth does not use stored VM as active guard", offline_existing_retry.handled),
                check("offline live truth retry asks for specs", "cpu" in offline_existing_retry.response.lower() and "ram" in offline_existing_retry.response.lower()),
                check("offline live truth retry avoids active guard", "already active" not in offline_existing_retry.response.lower()),
            ]
        )

        pending_live_queue = FakeProvisioningJobQueue(live_verify_status="pending")
        pending_live_service = ProvisioningChatService(temp_dir / "pending_live_sessions.sqlite3", job_queue=pending_live_queue)
        pending_live_service.handle("user-pending-live", "I want a web server", route_intent="provisioning")
        pending_live_specs = pending_live_service.handle("user-pending-live", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        pending_live_approval_id = pending_live_specs.metadata["provisioning"]["approval_id"]
        pending_live_service.handle("user-pending-live", f"approve {pending_live_approval_id}", route_intent=None)
        pending_live_service.handle("user-pending-live", "continue provisioning", route_intent=None)
        pending_live_queue.write_status("job-0001", "completed")
        pending_live_queue.write_result(
            ProvisioningJobResult(
                job_id="job-0001",
                instance_id="ava-web-timeout",
                instance_name="ava-web-timeout",
                ssh_host="127.0.0.1",
                ssh_port=2222,
                http_port=8080,
                verification_evidence={"checks": [{"name": "host_http_200", "passed": True, "evidence": "old HTTP 200"}]},
                completion_timestamp="2026-05-02T00:28:00+00:00",
                error=None,
            )
        )
        pending_live_retry = pending_live_service.handle("user-pending-live", "I want a web server", route_intent="provisioning")
        failures.extend(
            [
                check("unconfirmed live truth does not use stored VM as active guard", pending_live_retry.handled),
                check("unconfirmed live truth retry asks for specs", "cpu" in pending_live_retry.response.lower() and "ram" in pending_live_retry.response.lower()),
                check("unconfirmed live truth retry avoids active guard", "already active" not in pending_live_retry.response.lower()),
            ]
        )

        failed_service = ProvisioningChatService(temp_dir / "failed_retry_sessions.sqlite3", job_queue=job_queue)
        failed_service.handle("user-failed", "I want a web server", route_intent="provisioning")
        failed_specs = failed_service.handle("user-failed", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        failed_approval_id = failed_specs.metadata["provisioning"]["approval_id"]
        failed_service.handle("user-failed", f"approve {failed_approval_id}", route_intent=None)
        failed_service.handle("user-failed", "continue provisioning", route_intent=None)
        failed_session = failed_service.sessions.list_active("user-failed")[0]
        failed_job_id = failed_session.collected_answers["runner_job_id"]
        job_queue.write_status(failed_job_id, "failed")
        job_queue.write_result(
            ProvisioningJobResult(
                job_id=failed_job_id,
                instance_id="ava-web-failed",
                instance_name=None,
                ssh_host=None,
                ssh_port=None,
                http_port=None,
                verification_evidence={"rollback": {"status": "destroyed"}},
                completion_timestamp="2026-05-02T00:30:00+00:00",
                error={
                    "failed_step": "host_runner",
                    "failure_class": "runner_failed",
                    "message": "DNS resolution not ready for Ubuntu package mirrors",
                    "rollback": {"status": "destroyed"},
                },
            )
        )
        failed_status = failed_service.handle("user-failed", "show status", route_intent=None)
        failed_retry = failed_service.handle("user-failed", "I want a web server", route_intent="provisioning")
        failures.extend(
            [
                check("failed runner status is reported as failed", "phase: `failed`" in failed_status.response.lower()),
                check("failed runner status shows failure details", "failure details" in failed_status.response.lower()),
                check("failed runner status does not claim web hardening success", "nginx web_server role verified" not in failed_status.response.lower()),
                check("failed provisioning does not block retry", failed_retry.handled),
                check("failed provisioning retry starts a new request", "cpu" in failed_retry.response.lower() and "ram" in failed_retry.response.lower()),
                check("failed provisioning retry avoids active guard", "already active" not in failed_retry.response.lower()),
            ]
        )
        stale_db = temp_dir / "stale_retry_sessions.sqlite3"
        stale_queue = FakeProvisioningJobQueue()
        stale_service = ProvisioningChatService(stale_db, job_queue=stale_queue)
        stale_service.handle("user-stale", "I want a web server", route_intent="provisioning")
        stale_specs = stale_service.handle("user-stale", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        stale_approval_id = stale_specs.metadata["provisioning"]["approval_id"]
        stale_service.handle("user-stale", f"approve {stale_approval_id}", route_intent=None)
        stale_service.handle("user-stale", "continue provisioning", route_intent=None)
        stale_session = stale_service.sessions.list_active("user-stale")[0]
        stale_job_id = stale_session.collected_answers["runner_job_id"]
        stale_queue.statuses.pop(stale_job_id, None)
        stale_queue.results.pop(stale_job_id, None)
        with sqlite3.connect(stale_db) as conn:
            conn.execute(
                "UPDATE provisioning_sessions SET updated_at = ? WHERE session_id = ?",
                ("2026-05-02T00:00:00+00:00", stale_session.session_id),
            )
        stale_status = stale_service.handle("user-stale", "show status", route_intent=None)
        stale_retry = stale_service.handle("user-stale", "I want a web server", route_intent="provisioning")
        failures.extend(
            [
                check("expired redis job state is reported as failed", "phase: `failed`" in stale_status.response.lower()),
                check("expired redis job state explains stale runner truth", "runner job state expired" in stale_status.response.lower()),
                check("expired redis job no longer blocks retry", stale_retry.handled),
                check("expired redis retry starts a clean request", "cpu" in stale_retry.response.lower() and "ram" in stale_retry.response.lower()),
                check("expired redis retry avoids active guard", "already active" not in stale_retry.response.lower()),
            ]
        )

        stale_attached_db = temp_dir / "stale_attached_sessions.sqlite3"
        stale_attached_queue = FakeProvisioningJobQueue()
        stale_attached_service = ProvisioningChatService(stale_attached_db, job_queue=stale_attached_queue)
        stale_attached_service.handle("user-stale-attached", "I want a web server", route_intent="provisioning")
        stale_attached_specs = stale_attached_service.handle("user-stale-attached", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        stale_attached_approval_id = stale_attached_specs.metadata["provisioning"]["approval_id"]
        stale_attached_service.handle("user-stale-attached", f"approve {stale_attached_approval_id}", route_intent=None)
        stale_attached_service.handle("user-stale-attached", "continue provisioning", route_intent=None)
        stale_attached_session = stale_attached_service.sessions.list_active("user-stale-attached")[0]
        stale_attached_job_id = stale_attached_session.collected_answers["runner_job_id"]
        stale_attached_queue.statuses.pop(stale_attached_job_id, None)
        stale_attached_queue.results.pop(stale_attached_job_id, None)
        with sqlite3.connect(stale_attached_db) as conn:
            conn.execute(
                "UPDATE provisioning_sessions SET phase = ?, instance_id = ?, updated_at = ? WHERE session_id = ?",
                (
                    SessionPhase.AWAITING_POST_LOGIN_CHOICES.value,
                    "ava-web-gone",
                    "2026-05-02T00:00:00+00:00",
                    stale_attached_session.session_id,
                ),
            )
        stale_attached_status = stale_attached_service.handle("user-stale-attached", "show status", route_intent=None)
        stale_attached_retry = stale_attached_service.handle("user-stale-attached", "I want a web server", route_intent="provisioning")
        failures.extend(
            [
                check("expired attached session is reported as failed", "phase: `failed`" in stale_attached_status.response.lower()),
                check("expired attached session explains stale runner truth", "runner job state expired" in stale_attached_status.response.lower()),
                check("expired attached session no longer blocks retry", stale_attached_retry.handled),
                check("expired attached retry starts a clean request", "cpu" in stale_attached_retry.response.lower() and "ram" in stale_attached_retry.response.lower()),
                check("expired attached retry avoids active guard", "already active" not in stale_attached_retry.response.lower()),
            ]
        )

        orphaned_db = temp_dir / "orphaned_runner_sessions.sqlite3"
        orphaned_queue = FakeProvisioningJobQueue()
        orphaned_service = ProvisioningChatService(orphaned_db, job_queue=orphaned_queue)
        orphaned_service.handle("user-orphaned", "I want a web server", route_intent="provisioning")
        orphaned_specs = orphaned_service.handle("user-orphaned", "2 CPU, 4 GB RAM, 30 GB disk", route_intent=None)
        orphaned_approval_id = orphaned_specs.metadata["provisioning"]["approval_id"]
        orphaned_service.handle("user-orphaned", f"approve {orphaned_approval_id}", route_intent=None)
        orphaned_service.handle("user-orphaned", "continue provisioning", route_intent=None)
        orphaned_session = orphaned_service.sessions.list_active("user-orphaned")[0]
        orphaned_job_id = orphaned_session.collected_answers["runner_job_id"]
        orphaned_queue.write_status(orphaned_job_id, "bootstrapping")
        orphaned_queue.runner_healthy = False
        orphaned_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        with sqlite3.connect(orphaned_db) as conn:
            conn.execute(
                "UPDATE provisioning_sessions SET updated_at = ? WHERE session_id = ?",
                (orphaned_updated_at, orphaned_session.session_id),
            )
        orphaned_status = orphaned_service.handle("user-orphaned", "show status", route_intent=None)
        orphaned_retry = orphaned_service.handle("user-orphaned", "I want a web server", route_intent="provisioning")
        failures.extend(
            [
                check("orphaned runner job is reported as failed", "phase: `failed`" in orphaned_status.response.lower()),
                check("orphaned runner failure explains stopped runner", "runner stopped before ava received" in orphaned_status.response.lower()),
                check("orphaned runner job no longer blocks retry", orphaned_retry.handled),
                check("orphaned runner retry starts a clean request", "cpu" in orphaned_retry.response.lower() and "ram" in orphaned_retry.response.lower()),
                check("orphaned runner retry avoids active guard", "already active" not in orphaned_retry.response.lower()),
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
