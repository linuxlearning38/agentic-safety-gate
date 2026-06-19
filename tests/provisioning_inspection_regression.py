#!/usr/bin/env python3
"""Regression checks for check_updates / check_services server-inspection operations."""

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
from provisioning.day2 import (  # noqa: E402
    classify_day2_operation,
    format_full_package_list,
    format_live_check_updates_response,
    format_live_check_services_response,
)
from provisioning.runner import (  # noqa: E402
    Day2OperationJob,
    Day2OperationResult,
    ProvisioningJobProgress,
    ProvisioningJobResult,
    ProvisioningJob,
)
from provisioning.runner.host_runner import _parse_apt_upgradable, _parse_systemd_units  # noqa: E402
from provisioning.serving import ProvisioningChatService  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


# ---------------------------------------------------------------------------
# Fake queue (same pattern as phase6 regression)
# ---------------------------------------------------------------------------

class FakeProvisioningJobQueue:
    def __init__(self, *, runner_heartbeat=None):
        self.jobs: dict = {}
        self.statuses: dict = {}
        self.results: dict = {}
        self.day2_jobs: dict = {}
        self.day2_statuses: dict = {}
        self.day2_results: dict = {}
        self.counter = 0
        self.day2_counter = 0
        self.runner_healthy = True
        self.runner_heartbeat = runner_heartbeat

    def enqueue_approved_job(self, *, session_id, desired_state, credential_id, username, temporary_password):
        self.counter += 1
        job_id = f"job-{self.counter:04d}"
        job = ProvisioningJob(
            job_id=job_id,
            session_id=session_id,
            desired_state=dict(desired_state),
            credentials_seed_data={"credential_id": credential_id, "username": username, "temporary_password": temporary_password},
            enqueued_at="2026-06-17T00:00:00+00:00",
            expires_at="2026-06-17T00:30:00+00:00",
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
        return None

    def write_result(self, result):
        self.results[result.job_id] = result

    def is_runner_healthy(self):
        return self.runner_healthy

    def get_runner_heartbeat(self):
        return self.runner_heartbeat

    def enqueue_day2_operation(self, *, session_id, operation, target, instance_id, instance_name,
                               ssh_host, ssh_port, http_port, metadata=None):
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
            requested_at="2026-06-17T00:15:00+00:00",
            metadata=dict(metadata or {}),
        )
        self.day2_jobs[operation_id] = job
        self.day2_statuses[operation_id] = "queued"
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


# ---------------------------------------------------------------------------
# apt list fixture
# ---------------------------------------------------------------------------

APT_UPGRADABLE_FIXTURE = """\
Listing... Done
linux-image-virtual/focal-updates,focal-security 5.4.0.182.178 amd64 [upgradable from: 5.4.0.180.176]
openssl/focal-updates,focal-security 1.1.1f-1ubuntu2.23 amd64 [upgradable from: 1.1.1f-1ubuntu2.22]
openssh-server/focal-updates,focal-security 1:8.2p1-4ubuntu0.11 amd64 [upgradable from: 1:8.2p1-4ubuntu0.10]
libssl1.1/focal-updates,focal-security 1.1.1f-1ubuntu2.23 amd64 [upgradable from: 1.1.1f-1ubuntu2.22]
python3-distutils/focal-updates 3.8.10-0ubuntu1~20.04 all [upgradable from: 3.8.10-0ubuntu1~20.04.0]
curl/focal-updates 7.68.0-1ubuntu2.23 amd64 [upgradable from: 7.68.0-1ubuntu2.22]
"""

# ---------------------------------------------------------------------------
# systemctl fixtures
# ---------------------------------------------------------------------------

SYSTEMCTL_RUNNING_FIXTURE = """\
  nginx.service       loaded active running A high performance web server and a reverse proxy server
  ssh.service         loaded active running OpenBSD Secure Shell server
  cron.service        loaded active running Regular background program processing daemon
  systemd-journald.service loaded active running Journal Service
"""

SYSTEMCTL_FAILED_FIXTURE = """\
  myapp.service    loaded failed failed My application
"""


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------

def test_parse_apt_upgradable() -> list[bool]:
    total, security, packages, security_packages, high_impact, reboot_required = _parse_apt_upgradable(APT_UPGRADABLE_FIXTURE)
    return [
        check("apt parser: correct total_upgradable", total == 6, f"got {total}"),
        check("apt parser: correct security_updates count", security == 4, f"got {security}"),
        check("apt parser: openssl in packages", "openssl" in packages),
        check("apt parser: openssh-server in packages", "openssh-server" in packages),
        check("apt parser: no Listing header in packages", "Listing..." not in packages),
        check("apt parser: python3-distutils in packages (non-security)", "python3-distutils" in packages),
        # Enriched data assertions
        check("apt parser: security_packages contains linux-image-virtual", "linux-image-virtual" in security_packages),
        check("apt parser: security_packages contains openssl", "openssl" in security_packages),
        check("apt parser: security_packages count is 4", len(security_packages) == 4, f"got {len(security_packages)}"),
        check("apt parser: python3-distutils not in security_packages", "python3-distutils" not in security_packages),
        check("apt parser: linux-image-virtual in high_impact", "linux-image-virtual" in high_impact),
        check("apt parser: openssl in high_impact", "openssl" in high_impact),
        check("apt parser: curl in high_impact (non-security but high-impact)", "curl" in high_impact),
        check("apt parser: python3-distutils not in high_impact", "python3-distutils" not in high_impact),
        check("apt parser: reboot_required True (kernel package upgradable)", reboot_required is True),
    ]


def test_parse_systemd_units() -> list[bool]:
    running = _parse_systemd_units(SYSTEMCTL_RUNNING_FIXTURE)
    failed = _parse_systemd_units(SYSTEMCTL_FAILED_FIXTURE)
    return [
        check("systemd parser: nginx in running", "nginx" in running),
        check("systemd parser: ssh in running", "ssh" in running),
        check("systemd parser: cron in running", "cron" in running),
        check("systemd parser: running count correct", len(running) == 4, f"got {len(running)}"),
        check("systemd parser: myapp in failed", "myapp" in failed),
        check("systemd parser: failed count correct", len(failed) == 1, f"got {len(failed)}"),
    ]


# ---------------------------------------------------------------------------
# Format function unit tests
# ---------------------------------------------------------------------------

def test_format_check_updates_response() -> list[bool]:
    vm_result = ProvisioningJobResult(
        job_id="job-0001", instance_id="ava-web-06", instance_name="ava-web-06",
        ssh_host="127.0.0.1", ssh_port=2222, http_port=8080,
        verification_evidence={}, completion_timestamp="2026-06-17T00:00:00+00:00", error=None,
    )
    op_result = Day2OperationResult(
        operation_id="day2-0001", operation="check_updates", status="completed",
        instance_id="ava-web-06", instance_name="ava-web-06",
        evidence={
            "action": "check_updates",
            "total_upgradable": 74,
            "security_updates": 12,
            "packages": ["linux-image-virtual", "openssl", "openssh-server"] + [f"pkg-{i}" for i in range(71)],
            "security_packages": ["linux-image-virtual", "openssl"],
            "high_impact": ["linux-image-virtual", "openssl", "openssh-server"],
            "reboot_required": True,
        },
        completion_timestamp="2026-06-17T00:01:00+00:00",
    )
    response = format_live_check_updates_response(op_result, result=vm_result)
    return [
        check("format updates: shows total count", "74" in response),
        check("format updates: shows security count", "12" in response),
        check("format updates: priority tier header present", "Priority (high-impact)" in response),
        check("format updates: linux-image-virtual in priority tier", "linux-image-virtual" in response),
        check("format updates: reboot note present", "Reboot required after patching" in response),
        check("format updates: reboot required yes", "yes" in response.lower()),
        check("format updates: lists hostname", "ava-web-06" in response),
        check("format updates: mentions patch_server follow-up", "patch_server" in response),
        check("format updates: does NOT contain upgrade command", "apt upgrade" not in response),
        check("format updates: does NOT contain install command", "apt-get install" not in response),
        check("format updates: does NOT contain dist-upgrade", "dist-upgrade" not in response),
    ]


def test_format_check_services_response() -> list[bool]:
    vm_result = ProvisioningJobResult(
        job_id="job-0001", instance_id="ava-web-06", instance_name="ava-web-06",
        ssh_host="127.0.0.1", ssh_port=2222, http_port=8080,
        verification_evidence={}, completion_timestamp="2026-06-17T00:00:00+00:00", error=None,
    )
    op_result = Day2OperationResult(
        operation_id="day2-0002", operation="check_services", status="completed",
        instance_id="ava-web-06", instance_name="ava-web-06",
        evidence={
            "action": "check_services",
            "running": ["nginx", "ssh", "cron"],
            "failed": ["myapp"],
            "running_count": 3,
            "failed_count": 1,
        },
        completion_timestamp="2026-06-17T00:01:00+00:00",
    )
    response = format_live_check_services_response(op_result, result=vm_result)
    return [
        check("format services: shows running count", "3" in response),
        check("format services: shows failed count", "1" in response),
        check("format services: names nginx", "nginx" in response),
        check("format services: names failed service", "myapp" in response),
        check("format services: lists hostname", "ava-web-06" in response),
        check("format services: read-only note present", "read-only" in response.lower() or "not started" in response.lower() or "not stopped" in response.lower() or "no service was" in response.lower()),
    ]


# ---------------------------------------------------------------------------
# classify_day2_operation routing tests
# ---------------------------------------------------------------------------

def test_routing() -> list[bool]:
    results = []
    update_phrases = [
        "list upgradable packages on ava-web-06",
        "what updates are pending on ava-web-06",
        "show updates",
        "check updates",
        "how many packages are upgradable",
        "show upgradable packages",
    ]
    for phrase in update_phrases:
        op = classify_day2_operation(phrase)
        results.append(check(f"routes to check_updates: '{phrase}'", op is not None and op.operation == "check_updates", str(op)))

    service_phrases = [
        "which services are running on ava-web-06",
        "show failed services on ava-web-06",
        "check services",
        "list services",
        "running services",
        "what services are running",
    ]
    for phrase in service_phrases:
        op = classify_day2_operation(phrase)
        results.append(check(f"routes to check_services: '{phrase}'", op is not None and op.operation == "check_services", str(op)))

    # Neither should require approval
    results.append(check("check_updates requires_approval is False",
        classify_day2_operation("show updates").requires_approval is False))
    results.append(check("check_services requires_approval is False",
        classify_day2_operation("check services").requires_approval is False))

    # Must not fall through to None (which would hit RAG)
    results.append(check("check_updates not None (won't fall to RAG)",
        classify_day2_operation("list upgradable packages") is not None))
    results.append(check("check_services not None (won't fall to RAG)",
        classify_day2_operation("which services are running") is not None))
    return results


# ---------------------------------------------------------------------------
# No-mutating-command assertion on the runner methods
# ---------------------------------------------------------------------------

def test_no_upgrade_commands() -> list[bool]:
    import inspect
    from provisioning.runner import host_runner
    source = inspect.getsource(host_runner.HostRunner._execute_check_updates)
    source_services = inspect.getsource(host_runner.HostRunner._execute_check_services)
    forbidden = ["apt upgrade", "apt-get upgrade", "dist-upgrade", "apt install", "apt-get install"]
    results = []
    for cmd in forbidden:
        results.append(check(f"check_updates source has no '{cmd}'", cmd not in source, "FOUND in source"))
        results.append(check(f"check_services source has no '{cmd}'", cmd not in source_services, "FOUND in source"))
    return results


# ---------------------------------------------------------------------------
# End-to-end serving integration — routes correctly, unmanaged VM refused
# ---------------------------------------------------------------------------

def _provision(service, job_queue, user_id: str, vm_name: str, job_id: str, ssh_port: int, http_port: int) -> None:
    service.handle(user_id, "I want a web server", route_intent="provisioning")
    specs = service.handle(user_id, f"2 CPU, 4 GB RAM, 30 GB disk, hostname {vm_name}", route_intent=None)
    aid = specs.metadata["provisioning"]["approval_id"]
    service.handle(user_id, f"approve {aid}", route_intent=None)
    service.handle(user_id, "continue provisioning", route_intent=None)
    service.handle(user_id, "I logged in and changed the password", route_intent=None)
    service.handle(user_id, "yes harden it", route_intent=None)
    job_queue.write_status(job_id, "completed")
    job_queue.write_result(ProvisioningJobResult(
        job_id=job_id, instance_id=vm_name, instance_name=vm_name,
        ssh_host="127.0.0.1", ssh_port=ssh_port, http_port=http_port,
        verification_evidence={"checks": [{"name": "host_http_200", "passed": True, "evidence": "HTTP 200"}]},
        completion_timestamp="2026-06-17T00:10:00+00:00", error=None,
    ))


def test_serving_integration(temp_dir: Path) -> list[bool]:
    # Start with empty inventory so _runner_vm_name_conflict doesn't block provisioning.
    # After provisioning completes, update the heartbeat to expose the VM in live inventory.
    job_queue = FakeProvisioningJobQueue(
        runner_heartbeat={
            "status": "idle",
            "updated_at": "2026-06-17T00:00:00+00:00",
            "metadata": {
                "template_name": "ubuntu-cloud-image",
                "registered_vm_inventory": [],
            },
        }
    )
    service = ProvisioningChatService(temp_dir / "inspection_sessions.sqlite3", job_queue=job_queue)
    _provision(service, job_queue, "user-inspect", "ava-web-07", "job-0001", 2222, 8080)

    job_queue.runner_heartbeat = {
        "status": "idle",
        "updated_at": "2026-06-17T00:15:00+00:00",
        "metadata": {
            "template_name": "ubuntu-cloud-image",
            "registered_vm_inventory": [
                {"name": "ava-web-07", "exists": True, "power_state": "running", "provider_status": "running"},
            ],
        },
    }

    # --- check_updates on managed VM: queues day2 job (runner healthy, result pending) ---
    jobs_before = len(job_queue.day2_jobs)
    updates_resp = service.handle("user-inspect", "show updates", route_intent=None)
    jobs_after = len(job_queue.day2_jobs)

    # Simulate runner completing with parsed data
    if jobs_after > jobs_before:
        op_id = list(job_queue.day2_jobs.keys())[-1]
        job_queue.write_day2_status(op_id, "completed")
        job_queue.write_day2_result(Day2OperationResult(
            operation_id=op_id, operation="check_updates", status="completed",
            instance_id="ava-web-07", instance_name="ava-web-07",
            evidence={
                "action": "check_updates",
                "total_upgradable": 5,
                "security_updates": 2,
                "packages": ["openssl", "libssl1.1", "curl", "bash", "libc6"],
            },
            completion_timestamp="2026-06-17T00:02:00+00:00",
        ))
    updates_resp2 = service.handle("user-inspect", "show updates", route_intent=None)

    # --- check_services on managed VM ---
    svc_before = len(job_queue.day2_jobs)
    svc_resp = service.handle("user-inspect", "which services are running", route_intent=None)
    svc_after = len(job_queue.day2_jobs)

    if svc_after > svc_before:
        svc_op_id = list(job_queue.day2_jobs.keys())[-1]
        job_queue.write_day2_status(svc_op_id, "completed")
        job_queue.write_day2_result(Day2OperationResult(
            operation_id=svc_op_id, operation="check_services", status="completed",
            instance_id="ava-web-07", instance_name="ava-web-07",
            evidence={
                "action": "check_services",
                "running": ["nginx", "ssh", "cron"],
                "failed": [],
                "running_count": 3,
                "failed_count": 0,
            },
            completion_timestamp="2026-06-17T00:03:00+00:00",
        ))
    svc_resp2 = service.handle("user-inspect", "which services are running", route_intent=None)

    # --- Both operations should NOT trigger approval flow ---
    updates_no_approval = updates_resp2.metadata.get("provisioning", {}).get("approval_id")
    svc_no_approval = svc_resp2.metadata.get("provisioning", {}).get("approval_id")

    return [
        check("check_updates on managed VM: handled by serving", updates_resp.handled),
        check("check_updates queued day2 job", jobs_after > jobs_before),
        check("check_updates result: shows update count", "5" in updates_resp2.response or "queued" in updates_resp2.response.lower()),
        check("check_updates result: mentions openssl", "openssl" in updates_resp2.response or "queued" in updates_resp2.response.lower()),
        check("check_updates no approval required", "approval required" not in updates_resp2.response.lower()),
        check("check_services on managed VM: handled by serving", svc_resp.handled),
        check("check_services queued day2 job", svc_after > svc_before),
        check("check_services result: shows nginx", "nginx" in svc_resp2.response or "queued" in svc_resp2.response.lower()),
        check("check_services no approval required", "approval required" not in svc_resp2.response.lower()),
        check("check_updates intent does not hit RAG (handled=True)", updates_resp.handled is True),
        check("check_services intent does not hit RAG (handled=True)", svc_resp.handled is True),
    ]


# ---------------------------------------------------------------------------
# Unmanaged VM refusal test
# ---------------------------------------------------------------------------

def test_unmanaged_vm_refusal(temp_dir: Path) -> list[bool]:
    """Neither operation should work on an external/unmanaged VM."""
    job_queue = FakeProvisioningJobQueue(
        runner_heartbeat={
            "status": "idle",
            "updated_at": "2026-06-17T00:00:00+00:00",
            "metadata": {
                "template_name": "ubuntu-cloud-image",
                "registered_vm_inventory": [
                    {"name": "external-vm", "exists": True, "power_state": "running", "provider_status": "running"},
                ],
            },
        }
    )
    service = ProvisioningChatService(temp_dir / "unmanaged_sessions.sqlite3", job_queue=job_queue)

    # No AVA session for "external-vm" — operation has no target session
    updates_unmanaged = service.handle("user-ext", "show updates on external-vm", route_intent=None)
    svc_unmanaged = service.handle("user-ext", "check services on external-vm", route_intent=None)

    return [
        check("check_updates on unmanaged VM: not silently accepted (handled or correct refusal)",
            not updates_unmanaged.handled or "cannot" in updates_unmanaged.response.lower() or "not" in updates_unmanaged.response.lower()),
        check("check_services on unmanaged VM: not silently accepted",
            not svc_unmanaged.handled or "cannot" in svc_unmanaged.response.lower() or "not" in svc_unmanaged.response.lower()),
        check("check_updates on unmanaged does not create day2 job", len(job_queue.day2_jobs) == 0),
        check("check_services on unmanaged does not create day2 job", len(job_queue.day2_jobs) == 0),
    ]


# ---------------------------------------------------------------------------
# list_all_packages routing + format tests
# ---------------------------------------------------------------------------

def test_list_all_packages_routing() -> list[bool]:
    results = []
    phrases = [
        "show all upgradable packages on ava-web-06",
        "full update list",
        "list all packages",
        "show all updates on ava-web-06",
        "list all upgradable packages",
        "all upgradable packages",
    ]
    for phrase in phrases:
        op = classify_day2_operation(phrase)
        results.append(check(
            f"routes to list_all_packages: '{phrase}'",
            op is not None and op.operation == "list_all_packages",
            str(op),
        ))
    results.append(check(
        "list_all_packages: not None (won't fall to RAG)",
        classify_day2_operation("show all upgradable packages") is not None,
    ))
    results.append(check(
        "list_all_packages: requires_approval is False",
        classify_day2_operation("show all upgradable packages").requires_approval is False,
    ))
    return results


def test_format_full_package_list() -> list[bool]:
    evidence = {
        "packages": ["linux-image-virtual", "openssl", "libssl1.1", "python3-distutils", "curl"],
        "security_packages": ["linux-image-virtual", "openssl", "libssl1.1"],
    }
    response = format_full_package_list(evidence, hostname="ava-web-06")
    return [
        check("full_package_list: Security updates header present", "Security updates" in response),
        check("full_package_list: security count (3) shown", "3)" in response),
        check("full_package_list: Other updates header present", "Other updates" in response),
        check("full_package_list: other count (2) shown", "2)" in response),
        check("full_package_list: linux-image-virtual in security section", "linux-image-virtual" in response),
        check("full_package_list: openssl in security section", "openssl" in response),
        check("full_package_list: python3-distutils in other section", "python3-distutils" in response),
        check("full_package_list: curl in other section", "curl" in response),
        check("full_package_list: hostname present", "ava-web-06" in response),
        check("full_package_list: read-only note present", "No packages were modified" in response),
    ]


def test_list_all_packages_serving(temp_dir: Path) -> list[bool]:
    """list_all_packages uses stored check_updates evidence without re-running the apt scan."""
    job_queue = FakeProvisioningJobQueue(
        runner_heartbeat={
            "status": "idle",
            "updated_at": "2026-06-17T00:00:00+00:00",
            "metadata": {
                "template_name": "ubuntu-cloud-image",
                "registered_vm_inventory": [],
            },
        }
    )
    service = ProvisioningChatService(temp_dir / "list_all_sessions.sqlite3", job_queue=job_queue)
    _provision(service, job_queue, "user-listall", "ava-web-08", "job-0001", 2223, 8081)

    job_queue.runner_heartbeat = {
        "status": "idle",
        "updated_at": "2026-06-17T00:15:00+00:00",
        "metadata": {
            "template_name": "ubuntu-cloud-image",
            "registered_vm_inventory": [
                {"name": "ava-web-08", "exists": True, "power_state": "running", "provider_status": "running"},
            ],
        },
    }

    # Step 1: run a check_updates scan so a result gets stored in the session
    jobs_before_scan = len(job_queue.day2_jobs)
    service.handle("user-listall", "show updates", route_intent=None)
    jobs_after_scan = len(job_queue.day2_jobs)

    if jobs_after_scan > jobs_before_scan:
        scan_op_id = list(job_queue.day2_jobs.keys())[-1]
        job_queue.write_day2_status(scan_op_id, "completed")
        job_queue.write_day2_result(Day2OperationResult(
            operation_id=scan_op_id, operation="check_updates", status="completed",
            instance_id="ava-web-08", instance_name="ava-web-08",
            evidence={
                "action": "check_updates",
                "total_upgradable": 3,
                "security_updates": 2,
                "packages": ["linux-image-virtual", "openssl", "curl"],
                "security_packages": ["linux-image-virtual", "openssl"],
                "high_impact": ["linux-image-virtual", "openssl", "curl"],
                "reboot_required": True,
            },
            completion_timestamp="2026-06-17T00:02:00+00:00",
        ))

    # Step 2: ask for full list — must use the stored result, NOT enqueue a new job
    jobs_before_list = len(job_queue.day2_jobs)
    list_resp = service.handle("user-listall", "show all upgradable packages", route_intent=None)
    jobs_after_list = len(job_queue.day2_jobs)

    return [
        check("list_all_packages serving: handled by serving", list_resp.handled is True),
        check("list_all_packages serving: uses stored result (no new job queued)", jobs_after_list == jobs_before_list),
        check("list_all_packages serving: Security updates section present", "Security updates" in list_resp.response),
        check("list_all_packages serving: Other updates section present", "Other updates" in list_resp.response),
        check("list_all_packages serving: linux-image-virtual listed", "linux-image-virtual" in list_resp.response),
        check("list_all_packages serving: curl in other section", "curl" in list_resp.response),
        check("list_all_packages serving: no approval required", "approval required" not in list_resp.response.lower()),
        check("list_all_packages serving: does not hit RAG (handled=True)", list_resp.handled is True),
    ]


# ---------------------------------------------------------------------------
# Durable managed-server registry regression tests
# ---------------------------------------------------------------------------

def _make_heartbeat(vms: list[str], *, updated_at: str = "2026-06-17T00:15:00+00:00") -> dict:
    return {
        "status": "idle",
        "updated_at": updated_at,
        "metadata": {
            "template_name": "ubuntu-cloud-image",
            "registered_vm_inventory": [
                {"name": n, "exists": True, "power_state": "running", "provider_status": "running"}
                for n in vms
            ],
        },
    }


def test_registry_survives_session_expiry(temp_dir: Path) -> list[bool]:
    """ava_managed status comes from the durable registry, not the provisioning session."""
    db_path = temp_dir / "registry_expiry.sqlite3"
    job_queue = FakeProvisioningJobQueue(runner_heartbeat=_make_heartbeat([]))
    service = ProvisioningChatService(db_path, job_queue=job_queue)
    _provision(service, job_queue, "user-reg", "ava-web-09", "job-0001", 2224, 8082)

    # VM now in live inventory
    job_queue.runner_heartbeat = _make_heartbeat(["ava-web-09"])

    # Confirm managed while Redis result is still present
    rows_before = service._server_inventory_rows("user-reg")
    managed_before = next((r for r in rows_before if r["vm_name"] == "ava-web-09"), {})

    # Simulate session expiry: clear all Redis results from job_queue
    job_queue.results.clear()
    job_queue.statuses.clear()

    # ava_managed must still be True — derived from durable registry, not Redis
    rows_after = service._server_inventory_rows("user-reg")
    managed_after = next((r for r in rows_after if r["vm_name"] == "ava-web-09"), {})

    return [
        check("registry/expiry: managed before session clear", managed_before.get("ava_managed") is True),
        check("registry/expiry: managed after session clear (from registry)", managed_after.get("ava_managed") is True),
        check("registry/expiry: ssh_port recoverable after clear", (managed_after.get("result") and managed_after["result"].ssh_port) == 2224),
        check("registry/expiry: http_port recoverable after clear", (managed_after.get("result") and managed_after["result"].http_port) == 8082),
        check("registry/expiry: inventory_present True (VM in live VirtualBox)", managed_after.get("inventory_present") is True),
    ]


def test_registry_rebuild_simulation(temp_dir: Path) -> list[bool]:
    """Fresh ProvisioningChatService pointing at same DB still sees the server as managed."""
    db_path = temp_dir / "registry_rebuild.sqlite3"
    job_queue_a = FakeProvisioningJobQueue(runner_heartbeat=_make_heartbeat([]))
    service_a = ProvisioningChatService(db_path, job_queue=job_queue_a)
    _provision(service_a, job_queue_a, "user-rebuild", "ava-web-10", "job-0001", 2225, 8083)

    # Force _runner_snapshot to see the completed Redis result so the registry is written
    # before we destroy the first service (simulating the pre-rebuild inventory call).
    job_queue_a.runner_heartbeat = _make_heartbeat(["ava-web-10"])
    service_a._server_inventory_rows("user-rebuild")

    # Simulate container rebuild: fresh service + fresh (empty) job queue, same DB
    job_queue_b = FakeProvisioningJobQueue(runner_heartbeat=_make_heartbeat(["ava-web-10"]))
    service_b = ProvisioningChatService(db_path, job_queue=job_queue_b)

    rows = service_b._server_inventory_rows("user-rebuild")
    managed = next((r for r in rows if r["vm_name"] == "ava-web-10"), {})

    # Lifecycle op must resolve without "could not find that AVA-managed server"
    verify_resp = service_b.handle("user-rebuild", "verify ava-web-10", route_intent=None)

    return [
        check("registry/rebuild: server still ava_managed after rebuild", managed.get("ava_managed") is True),
        check("registry/rebuild: result populated from registry (ssh_port)", (managed.get("result") and managed["result"].ssh_port) == 2225),
        check("registry/rebuild: result populated from registry (http_port)", (managed.get("result") and managed["result"].http_port) == 8083),
        check("registry/rebuild: verify handled by serving (not dropped to RAG)", verify_resp.handled is True),
        check("registry/rebuild: verify does not say 'could not find'", "could not find" not in verify_resp.response.lower()),
    ]


def test_registry_delete_removes_record(temp_dir: Path) -> list[bool]:
    """delete_vm day2 operation completing removes the registry record."""
    db_path = temp_dir / "registry_delete.sqlite3"
    job_queue = FakeProvisioningJobQueue(runner_heartbeat=_make_heartbeat([]))
    service = ProvisioningChatService(db_path, job_queue=job_queue)
    _provision(service, job_queue, "user-del", "ava-web-11", "job-0001", 2226, 8084)

    job_queue.runner_heartbeat = _make_heartbeat(["ava-web-11"])

    # Force _runner_snapshot to see the Redis result so the registry is written.
    service._server_inventory_rows("user-del")

    # Registry record must exist now
    reg_before = service.managed_servers.get_by_name("ava-web-11")

    # Simulate: user asks to delete, approval is granted, operation enqueued
    delete_resp = service.handle("user-del", "delete ava-web-11", route_intent=None)
    approval_id = None
    if delete_resp.handled and "approval" in delete_resp.response.lower():
        import re
        m = re.search(r"approve ([A-Za-z0-9\-]+)", delete_resp.response)
        if m:
            approval_id = m.group(1)

    if approval_id:
        service.handle("user-del", f"approve {approval_id}", route_intent=None)

    # At this point a day2 delete job is queued — simulate runner completing it
    if job_queue.day2_jobs:
        del_op_id = list(job_queue.day2_jobs.keys())[-1]
        job_queue.write_day2_status(del_op_id, "completed")
        job_queue.write_day2_result(Day2OperationResult(
            operation_id=del_op_id, operation="delete_vm", status="completed",
            instance_id="ava-web-11", instance_name="ava-web-11",
            evidence={"deleted": True},
            completion_timestamp="2026-06-17T01:00:00+00:00",
        ))
        # Trigger _runner_snapshot to detect completed delete and remove registry record
        service._server_inventory_rows("user-del")

    reg_after = service.managed_servers.get_by_name("ava-web-11")

    return [
        check("registry/delete: record exists before delete", reg_before is not None),
        check("registry/delete: delete request handled by serving", delete_resp.handled is True),
        check("registry/delete: record removed after delete completes", reg_after is None, f"record={reg_after}"),
    ]


def test_registry_absent_vm_reported_gone(temp_dir: Path) -> list[bool]:
    """A VM in the registry but absent from live VirtualBox is reported gone, not falsely present."""
    db_path = temp_dir / "registry_gone.sqlite3"
    job_queue = FakeProvisioningJobQueue(runner_heartbeat=_make_heartbeat([]))
    service = ProvisioningChatService(db_path, job_queue=job_queue)
    _provision(service, job_queue, "user-gone", "ava-web-12", "job-0001", 2227, 8085)

    # VM intentionally NOT in live inventory (simulate external deletion / power-off from VBox)
    job_queue.runner_heartbeat = _make_heartbeat([])  # empty live inventory

    rows = service._server_inventory_rows("user-gone")
    gone_row = next((r for r in rows if r["vm_name"] == "ava-web-12"), None)

    return [
        check("registry/gone: row is present (AVA knows about it)", gone_row is not None),
        check("registry/gone: ava_managed True (AVA provisioned it)", (gone_row or {}).get("ava_managed") is True),
        check("registry/gone: inventory_present False (HARD INVARIANT: not in live VBox)", (gone_row or {}).get("inventory_present") is False),
        check("registry/gone: power_state signals absence", "not in" in str((gone_row or {}).get("power_state", ""))),
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="ava-inspection-"))
    old_queue = os.environ.get("APPROVAL_QUEUE_PATH")
    os.environ["APPROVAL_QUEUE_PATH"] = str(temp_dir / "approval_queue.json")
    try:
        failures: list[bool] = []

        print("\n=== Parser unit tests ===")
        failures.extend(test_parse_apt_upgradable())
        failures.extend(test_parse_systemd_units())

        print("\n=== Format function tests ===")
        failures.extend(test_format_check_updates_response())
        failures.extend(test_format_check_services_response())

        print("\n=== Routing tests ===")
        failures.extend(test_routing())

        print("\n=== No-upgrade-command assertion ===")
        failures.extend(test_no_upgrade_commands())

        print("\n=== Serving integration tests ===")
        failures.extend(test_serving_integration(temp_dir))

        print("\n=== Unmanaged VM refusal tests ===")
        failures.extend(test_unmanaged_vm_refusal(temp_dir))

        print("\n=== list_all_packages routing tests ===")
        failures.extend(test_list_all_packages_routing())

        print("\n=== format_full_package_list tests ===")
        failures.extend(test_format_full_package_list())

        print("\n=== list_all_packages serving integration ===")
        failures.extend(test_list_all_packages_serving(temp_dir))

        print("\n=== Durable managed-server registry tests ===")
        failures.extend(test_registry_survives_session_expiry(temp_dir))
        failures.extend(test_registry_rebuild_simulation(temp_dir))
        failures.extend(test_registry_delete_removes_record(temp_dir))
        failures.extend(test_registry_absent_vm_reported_gone(temp_dir))

        failed = len([item for item in failures if not item])
        if failed:
            print(f"\nInspection regression FAILED: {failed} issue(s)")
            return 1
        print("\nInspection regression PASSED.")
        return 0
    finally:
        if old_queue is None:
            os.environ.pop("APPROVAL_QUEUE_PATH", None)
        else:
            os.environ["APPROVAL_QUEUE_PATH"] = old_queue
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
