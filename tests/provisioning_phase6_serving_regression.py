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
from provisioning.runner import ProvisioningJob, ProvisioningJobResult  # noqa: E402
from provisioning.serving import ProvisioningChatService  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


class FakeProvisioningJobQueue:
    def __init__(self):
        self.jobs: dict[str, ProvisioningJob] = {}
        self.statuses: dict[str, str] = {}
        self.results: dict[str, ProvisioningJobResult] = {}
        self.counter = 0

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

    def write_result(self, result):
        self.results[result.job_id] = result


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
                check("chat approval queues runner job", "runner job queued" in approved.response.lower()),
                check("chat approval clarifies runner boundary", "host-side" in approved.response.lower()),
                check("chat approval explains putty endpoint later", "putty ssh host/ip" in approved.response.lower()),
                check("runner job contains temporary seed secret", bool(job_queue.jobs["job-0001"].credentials_seed_data.get("temporary_password"))),
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

        verify = service.handle("user-1", "verify the web server", route_intent=None)
        status = service.handle("user-1", "show me the provisioning status", route_intent=None)
        evidence = service.handle("user-1", "what did you do and what evidence do you have?", route_intent=None)
        failures.extend(
            [
                check("web verification follow-up stays in provisioning session", verify.handled),
                check("web verification reports queued runner honestly", "runner status" in verify.response.lower()),
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
        issued_secret = job_queue.jobs["job-0001"].credentials_seed_data["temporary_password"]
        failures.extend(
            [
                check("completed status shows putty ssh host", "ssh host/ip: `127.0.0.1`" in completed_status.response.lower()),
                check("completed status shows putty ssh port", "ssh port: `2222`" in completed_status.response.lower()),
                check("completed status reports effective phase", "phase: `completed`" in completed_status.response.lower()),
                check("completed status keeps conversation checkpoint visible", "conversation checkpoint:" in completed_status.response.lower()),
                check("completed verify shows http evidence", "http://127.0.0.1:8080/" in completed_verify.response.lower()),
                check("connection query shows putty host", "putty host name" in completed_connection.response.lower()),
                check("connection query shows username", "username: `avaadmin`" in completed_connection.response.lower()),
                check("connection query does not reprint actual password", issued_secret not in completed_connection.response),
                check("evidence includes hardening summary", "hardening summary" in completed_evidence.response.lower()),
                check("evidence reports effective completed phase", "effective phase: `completed`" in completed_evidence.response.lower()),
                check("evidence states apache not applied", "apache hardening" in completed_evidence.response.lower()),
            ]
        )

        late_start = service.handle("user-late", "I want a web server in Ubuntu", route_intent="provisioning")
        late_specs = service.handle("user-late", "2 CPU, 4 GB RAM, 30 GB disk, hostname ava-web-late", route_intent=None)
        late_approval_id = late_specs.metadata["provisioning"]["approval_id"]
        late_approved = service.handle("user-late", f"approve {late_approval_id}", route_intent=None)
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
                check("late login does not ask for hardening again", "reply `yes harden it`" not in late_login.response.lower()),
                check("late login reports runner already completed", "already completed" in late_login.response.lower()),
                check("late hardening does not claim next execution step", "next execution step" not in late_hardening.response.lower()),
                check("late hardening reports runner-applied baseline", "already applied" in late_hardening.response.lower()),
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
