"""Server-management operation routing for AVA-managed VMs.

This module is intentionally chat-safe: it classifies and formats server-management
operations, but it does not execute mutating actions directly. Mutating actions
must go through approval first, then a host-runner implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from provisioning.runner import ProvisioningJobResult


READ_ONLY_OPERATIONS = {"status", "verify", "nginx_logs"}
APPROVAL_OPERATIONS = {"restart_nginx", "snapshot", "rollback_snapshot", "stop_vm", "start_vm"}
HIGH_RISK_OPERATIONS = {"rollback_snapshot"}


@dataclass(slots=True)
class Day2Operation:
    """Classified server-management operation request."""

    operation: str
    target: str
    risk: str
    requires_approval: bool
    description: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_day2_operation(query: str) -> Day2Operation | None:
    """Classify a user query as a server-management operation if it targets an AVA VM."""

    normalized = re.sub(r"\s+", " ", (query or "").lower()).strip(" ?!.")
    if not normalized:
        return None

    if _is_status(normalized):
        return Day2Operation("status", "vm", "low", False, "show VM and service status")
    if _is_verify(normalized):
        return Day2Operation("verify", "web_server", "low", False, "verify web server health")
    if _is_nginx_logs(normalized):
        return Day2Operation("nginx_logs", "nginx", "low", False, "show recent nginx logs")
    if _is_restart_nginx(normalized):
        return Day2Operation("restart_nginx", "nginx", "medium", True, "restart nginx and verify HTTP")
    if _is_snapshot(normalized):
        return Day2Operation("snapshot", "virtualbox_vm", "medium", True, "take a VirtualBox snapshot")
    if _is_rollback(normalized):
        return Day2Operation("rollback_snapshot", "virtualbox_vm", "high", True, "roll back to the latest AVA snapshot")
    if _is_stop_vm(normalized):
        return Day2Operation("stop_vm", "virtualbox_vm", "medium", True, "stop the VM")
    if _is_start_vm(normalized):
        return Day2Operation("start_vm", "virtualbox_vm", "medium", True, "start the VM")
    return None


def format_read_only_response(operation: Day2Operation, *, session: Any, result: ProvisioningJobResult) -> str:
    """Format a safe read-only response from stored runner evidence."""

    if operation.operation == "status":
        return _format_status(session=session, result=result)
    if operation.operation == "verify":
        return _format_verify(session=session, result=result)
    if operation.operation == "nginx_logs":
        return _format_nginx_logs(session=session, result=result)
    raise ValueError(f"Unsupported read-only server-management operation: {operation.operation}")


def format_approval_required_response(
    operation: Day2Operation,
    *,
    session: Any,
    result: ProvisioningJobResult,
    approval_id: str,
) -> str:
    """Format the user-facing approval prompt for a mutating server-management operation."""

    warning = ""
    if operation.risk == "high":
        warning = (
            "\n\nHigh-risk warning: rollback may discard changes made after the latest AVA snapshot. "
            "AVA will require approval before this can proceed."
        )
    return (
        f"Approval required to {_human_action(operation)}.\n\n"
        f"- Operation: `{operation.operation}`\n"
        f"- Target: `{operation.target}`\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- Risk: `{operation.risk}`\n"
        f"- Approval ID: `{approval_id}`"
        f"{warning}\n\n"
        f"To approve, reply: `approve {approval_id}`.\n\n"
        "No change has been made yet."
    )


def format_approved_pending_response(operation: Day2Operation, *, session: Any, result: ProvisioningJobResult) -> str:
    """Format approved server-management operations before runner execution exists."""

    return (
        "Approval recorded.\n\n"
        f"- Operation: `{operation.operation}`\n"
        f"- Target: `{operation.target}`\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        "- Execution status: `queued for runner support`\n\n"
        "No VM or service change has been executed by this approval yet. AVA has recorded the approval, "
        "and the execution path will be enabled when server-management actions are connected to the "
        "Windows host runner."
    )


def _is_status(query: str) -> bool:
    return (
        "show status" in query
        or "server status" in query
        or "vm status" in query
        or "status of my web server" in query
        or "status of the web server" in query
    )


def _is_verify(query: str) -> bool:
    return "verify" in query and ("web server" in query or "nginx" in query or "server" in query)


def _is_nginx_logs(query: str) -> bool:
    return "log" in query and ("nginx" in query or "web server" in query)


def _is_restart_nginx(query: str) -> bool:
    return ("restart" in query or "reload" in query) and ("nginx" in query or "web server" in query)


def _is_snapshot(query: str) -> bool:
    return "snapshot" in query and not _is_rollback(query)


def _is_rollback(query: str) -> bool:
    return any(marker in query for marker in ("rollback", "roll back", "restore snapshot", "revert snapshot"))


def _is_stop_vm(query: str) -> bool:
    return any(marker in query for marker in ("stop vm", "stop the vm", "shutdown vm", "power off vm", "poweroff vm"))


def _is_start_vm(query: str) -> bool:
    return any(marker in query for marker in ("start vm", "start the vm", "boot vm", "power on vm"))


def _format_status(*, session: Any, result: ProvisioningJobResult) -> str:
    return (
        "Web server status:\n\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- Instance ID: `{result.instance_id}`\n"
        f"- SSH / PuTTY: `{result.ssh_host}:{result.ssh_port}`\n"
        f"- Web URL: `http://127.0.0.1:{result.http_port}/`\n"
        f"- Last runner status: `completed`\n"
        f"- Last verified: `{result.completion_timestamp}`\n"
        f"- Evidence timestamp: `{_utc_now()}`\n\n"
        "Current source of truth: stored runner result from the completed provisioning session. "
        "Live power and service checks will be added through the host runner in the next slice."
    )


def _format_verify(*, session: Any, result: ProvisioningJobResult) -> str:
    checks = (result.verification_evidence or {}).get("checks") or []
    check_lines = []
    for check in checks[:8]:
        if isinstance(check, dict):
            check_lines.append(
                f"- {check.get('name')}: `{'passed' if check.get('passed') else 'failed'}` ({check.get('evidence', '')})"
            )
    if not check_lines:
        check_lines = ["- runner verification evidence exists, but no detailed check list was stored"]
    return (
        "Web-server verification from stored runner evidence:\n\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- HTTP: `http://127.0.0.1:{result.http_port}/`\n"
        f"- Completed: `{result.completion_timestamp}`\n\n"
        + "\n".join(check_lines)
        + "\n\nLive re-verification through the host runner is the next implementation slice."
    )


def _format_nginx_logs(*, session: Any, result: ProvisioningJobResult) -> str:
    return (
        "Nginx log request recognized for the active AVA-managed web server.\n\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- SSH / PuTTY: `{result.ssh_host}:{result.ssh_port}`\n"
        "- Current evidence: nginx was active during runner verification\n"
        "- Live log retrieval: `not enabled yet`\n\n"
        "No SSH log command was executed from chat yet. AVA will run `journalctl -u nginx` and recent "
        "access/error log checks after live server-management actions are connected to the Windows host runner."
    )


def _human_action(operation: Day2Operation) -> str:
    labels = {
        "restart_nginx": "restart nginx",
        "snapshot": "take a VM snapshot",
        "rollback_snapshot": "roll back the VM to the latest AVA snapshot",
        "stop_vm": "stop the VM",
        "start_vm": "start the VM",
    }
    return labels.get(operation.operation, operation.description)
