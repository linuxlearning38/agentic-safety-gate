"""Server-management operation routing for AVA-managed VMs.

This module is intentionally chat-safe: it classifies and formats server-management
operations, but it does not execute mutating actions directly. Mutating actions
must go through approval first, then a host-runner implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from provisioning.runner import Day2OperationResult, ProvisioningJobResult


READ_ONLY_OPERATIONS = {"status", "verify", "nginx_logs", "open_ssh_console", "check_updates", "check_services", "list_all_packages"}
APPROVAL_OPERATIONS = {"restart_nginx", "snapshot", "rollback_snapshot", "stop_vm", "start_vm", "delete_vm"}
HIGH_RISK_OPERATIONS = {"rollback_snapshot", "delete_vm"}


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
    if _is_open_ssh_console(normalized):
        return Day2Operation("open_ssh_console", "ssh_console", "low", False, "open an SSH console")
    if _is_list_all_packages(normalized):
        return Day2Operation("list_all_packages", "guest_packages", "low", False, "list all upgradable packages grouped by type")
    if _is_check_updates(normalized):
        return Day2Operation("check_updates", "guest_packages", "low", False, "list pending package updates")
    if _is_check_services(normalized):
        return Day2Operation("check_services", "guest_services", "low", False, "list running and failed services")
    if _is_restart_nginx(normalized):
        return Day2Operation("restart_nginx", "nginx", "medium", True, "restart nginx and verify HTTP")
    if _is_snapshot(normalized):
        return Day2Operation("snapshot", "virtualbox_vm", "medium", True, "take a VirtualBox snapshot")
    if _is_rollback(normalized):
        return Day2Operation("rollback_snapshot", "virtualbox_vm", "high", True, "roll back to the latest AVA snapshot")
    if _is_delete_vm(normalized):
        return Day2Operation("delete_vm", "virtualbox_vm", "high", True, "delete the VM and its disk files")
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
    if operation.operation == "open_ssh_console":
        return _format_open_ssh_console(session=session, result=result)
    if operation.operation == "check_updates":
        return _format_check_updates_stored(session=session, result=result)
    if operation.operation == "check_services":
        return _format_check_services_stored(session=session, result=result)
    if operation.operation == "list_all_packages":
        return _format_list_all_packages_stored(session=session, result=result)
    raise ValueError(f"Unsupported read-only server-management operation: {operation.operation}")


def format_live_verify_response(operation_result: Day2OperationResult, *, result: ProvisioningJobResult) -> str:
    """Format fresh host-runner verification evidence for chat."""

    evidence = dict(operation_result.evidence or {})
    checks = list(evidence.get("checks") or [])
    check_lines: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = "passed" if check.get("passed") else "failed"
        check_lines.append(f"- {check.get('name', 'check')}: `{status}` ({check.get('evidence', '')})")
    if not check_lines:
        check_lines = ["- live verification completed, but no detailed check list was stored"]

    http_port = result.http_port or evidence.get("http_port")
    title = "Live web-server verification from the host runner:"
    error_lines: list[str] = []
    if operation_result.status != "completed":
        error = operation_result.error or {}
        title = "Live web-server verification could not confirm the server:"
        error_lines = [
            "",
            f"- Error: `{error.get('message') or error.get('failure_class') or 'live verification failed'}`",
        ]
    return (
        f"{title}\n\n"
        f"- VM: `{operation_result.instance_name or operation_result.instance_id}`\n"
        f"- SSH / PuTTY: `{result.ssh_host or evidence.get('ssh_host', 'unknown')}:{result.ssh_port or evidence.get('ssh_port', 'unknown')}`\n"
        f"- HTTP: `http://127.0.0.1:{http_port}/`\n"
        f"- Status: `{operation_result.status}`\n"
        f"- Verified: `{operation_result.completion_timestamp}`"
        + ("\n" + "\n".join(error_lines) if error_lines else "")
        + "\n\n"
        + "\n".join(check_lines)
    )


def format_live_nginx_logs_response(operation_result: Day2OperationResult, *, result: ProvisioningJobResult) -> str:
    """Format fresh nginx log evidence collected by the host runner."""

    evidence = dict(operation_result.evidence or {})
    if operation_result.status != "completed":
        error = operation_result.error or {}
        return (
            "Live nginx log retrieval could not complete.\n\n"
            f"- VM: `{operation_result.instance_name or operation_result.instance_id}`\n"
            f"- Status: `{operation_result.status}`\n"
            f"- Error: `{error.get('message') or error.get('failure_class') or 'log retrieval failed'}`\n"
            f"- Checked: `{operation_result.completion_timestamp}`"
        )

    return (
        "Live nginx logs from the host runner:\n\n"
        f"- VM: `{operation_result.instance_name or operation_result.instance_id}`\n"
        f"- SSH / PuTTY: `{result.ssh_host or evidence.get('ssh_host', 'unknown')}:{result.ssh_port or evidence.get('ssh_port', 'unknown')}`\n"
        f"- Status: `{operation_result.status}`\n"
        f"- Collected: `{operation_result.completion_timestamp}`\n\n"
        "Recent nginx service log:\n"
        "```text\n"
        f"{str(evidence.get('journalctl_tail') or '').strip()[:2500] or 'no journal output returned'}\n"
        "```\n\n"
        "Recent nginx access log:\n"
        "```text\n"
        f"{str(evidence.get('access_log_tail') or '').strip()[:1200] or 'no access log output returned'}\n"
        "```\n\n"
        "Recent nginx error log:\n"
        "```text\n"
        f"{str(evidence.get('error_log_tail') or '').strip()[:1200] or 'no error log output returned'}\n"
        "```"
    )


def format_open_ssh_console_response(operation_result: Day2OperationResult, *, result: ProvisioningJobResult) -> str:
    """Format evidence after the host runner launches a local SSH console."""

    evidence = dict(operation_result.evidence or {})
    if operation_result.status != "completed":
        error = operation_result.error or {}
        return (
            "SSH console launch could not complete.\n\n"
            f"- VM: `{operation_result.instance_name or operation_result.instance_id}`\n"
            f"- SSH / PuTTY: `{result.ssh_host or evidence.get('ssh_host', 'unknown')}:{result.ssh_port or evidence.get('ssh_port', 'unknown')}`\n"
            f"- Status: `{operation_result.status}`\n"
            f"- Error: `{error.get('message') or error.get('failure_class') or 'console launch failed'}`\n"
            f"- Checked: `{operation_result.completion_timestamp}`"
        )

    return (
        "SSH console launch requested through the Windows host runner.\n\n"
        f"- VM: `{operation_result.instance_name or operation_result.instance_id}`\n"
        f"- Tool: `{evidence.get('tool') or 'ssh'}`\n"
        f"- SSH / PuTTY: `{evidence.get('ssh_host', result.ssh_host or 'unknown')}:{evidence.get('ssh_port', result.ssh_port or 'unknown')}`\n"
        f"- Username: `{evidence.get('username') or 'avaadmin'}`\n"
        f"- Status: `{operation_result.status}`\n"
        f"- Launched: `{operation_result.completion_timestamp}`\n\n"
        "AVA does not pass or reprint the password. Type the VM password manually in the opened SSH window."
    )


def format_live_verify_queued_response(operation_id: str, *, result: ProvisioningJobResult) -> str:
    return (
        "Live verification has been queued for the Windows host runner.\n\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- Operation ID: `{operation_id}`\n"
        "- Status: `queued`\n\n"
        "Ask `verify the web server` again in a few seconds to see the fresh evidence."
    )


def format_live_nginx_logs_queued_response(operation_id: str, *, result: ProvisioningJobResult) -> str:
    return (
        "Live nginx log retrieval has been queued for the Windows host runner.\n\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- Operation ID: `{operation_id}`\n"
        "- Status: `queued`\n\n"
        "Ask `show nginx logs` again in a few seconds to see the fresh logs."
    )


def format_open_ssh_console_queued_response(operation_id: str, *, result: ProvisioningJobResult) -> str:
    return (
        "SSH console launch has been queued for the Windows host runner.\n\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- SSH / PuTTY: `{result.ssh_host}:{result.ssh_port}`\n"
        f"- Operation ID: `{operation_id}`\n"
        "- Status: `queued`\n\n"
        "If a console does not open shortly, ask `show status of my web server` for the latest operation evidence."
    )


def format_approval_required_response(
    operation: Day2Operation,
    *,
    session: Any,
    result: ProvisioningJobResult,
    approval_id: str,
) -> str:
    """Format the user-facing approval prompt for a mutating server-management operation."""

    warning = ""
    if operation.operation == "rollback_snapshot":
        warning = (
            "\n\nHigh-risk warning: rollback may discard changes made after the latest AVA snapshot. "
            "AVA will require approval before this can proceed."
        )
    elif operation.operation == "delete_vm":
        warning = (
            "\n\nHigh-risk warning: delete removes the VirtualBox VM and its virtual disk files. "
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


def format_approved_queued_response(
    operation: Day2Operation,
    *,
    session: Any,
    result: ProvisioningJobResult,
    operation_id: str,
) -> str:
    return (
        "Approval recorded. AVA has queued this operation for the Windows host runner.\n\n"
        f"- Operation: `{operation.operation}`\n"
        f"- Target: `{operation.target}`\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- Operation ID: `{operation_id}`\n"
        "- Execution status: `queued`\n\n"
        "Ask `show status of my web server` or `what did you do and what evidence do you have?` "
        "after the runner finishes to see the latest evidence."
    )


def _is_status(query: str) -> bool:
    return (
        "show status" in query
        or "server status" in query
        or "vm status" in query
        or "snapshot status" in query
        or "operation status" in query
        or "latest operation" in query
        or "latest snapshot" in query
        or "status of my web server" in query
        or "status of the web server" in query
    )


def _is_verify(query: str) -> bool:
    named_server = re.search(r"\b[a-z0-9][a-z0-9-]*(?:web|server)[a-z0-9-]*\b", query)
    return "verify" in query and (
        "web server" in query or "nginx" in query or "server" in query or bool(named_server)
    )


def _is_nginx_logs(query: str) -> bool:
    return "log" in query and ("nginx" in query or "web server" in query)


def _is_open_ssh_console(query: str) -> bool:
    informational = (
        query.startswith("how ")
        or query.startswith("what ")
        or query.startswith("show ")
        or "details" in query
    )
    if informational:
        return False
    return any(
        marker in query
        for marker in (
            "open putty",
            "launch putty",
            "start putty",
            "open ssh console",
            "open ssh terminal",
            "open ssh session",
            "open terminal",
            "open shell",
            "connect me to ssh",
            "connect to ssh",
        )
    )


def _is_restart_nginx(query: str) -> bool:
    return ("restart" in query or "reload" in query) and ("nginx" in query or "web server" in query)


def _is_snapshot(query: str) -> bool:
    return "snapshot" in query and not _is_rollback(query)


def _is_rollback(query: str) -> bool:
    return any(marker in query for marker in ("rollback", "roll back", "restore snapshot", "revert snapshot"))


def _is_stop_vm(query: str) -> bool:
    if re.match(r"^(stop|shutdown|power off|poweroff)\s+[a-z0-9][a-z0-9-]{1,60}$", query):
        return True
    return any(
        marker in query
        for marker in (
            "stop vm",
            "stop the vm",
            "stop server",
            "stop the server",
            "shutdown vm",
            "shutdown server",
            "power off vm",
            "power off server",
            "poweroff vm",
            "poweroff server",
        )
    )


def _is_start_vm(query: str) -> bool:
    if re.match(r"^(start|boot|power on)\s+[a-z0-9][a-z0-9-]{1,60}$", query):
        return True
    return any(
        marker in query
        for marker in (
            "start vm",
            "start the vm",
            "start server",
            "start the server",
            "boot vm",
            "boot server",
            "power on vm",
            "power on server",
        )
    )


def _is_delete_vm(query: str) -> bool:
    if re.match(r"^(delete|remove|destroy)\s+[a-z0-9][a-z0-9-]{1,60}$", query):
        return True
    return any(
        marker in query
        for marker in (
            "delete vm",
            "delete the vm",
            "delete server",
            "delete the server",
            "delete web server",
            "remove vm",
            "remove server",
            "destroy vm",
            "destroy server",
            "delete ava-web",
            "remove ava-web",
            "destroy ava-web",
        )
    )


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


def _format_open_ssh_console(*, session: Any, result: ProvisioningJobResult) -> str:
    return (
        "SSH console can be opened for the active AVA-managed web server.\n\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- SSH / PuTTY: `{result.ssh_host}:{result.ssh_port}`\n"
        "- Status: `ready for host-runner launch`\n\n"
        "Ask `open PuTTY` or `open SSH console` to launch it through the Windows host runner."
    )


def _is_list_all_packages(query: str) -> bool:
    return any(
        marker in query
        for marker in (
            "show all upgradable",
            "show all updates",
            "full update list",
            "list all packages",
            "list all upgradable",
            "all upgradable packages",
        )
    )


def _is_check_updates(query: str) -> bool:
    return any(
        marker in query
        for marker in (
            "upgradable packages",
            "upgradeable packages",
            "packages upgradable",
            "packages upgradeable",
            "how many packages",
            "updates pending",
            "pending updates",
            "what updates",
            "show updates",
            "list updates",
            "check updates",
            "available updates",
        )
    ) or ("update" in query and "pending" in query)


def _is_check_services(query: str) -> bool:
    return any(
        marker in query
        for marker in (
            "which services",
            "services running",
            "running services",
            "show services",
            "list services",
            "check services",
            "failed services",
            "show failed services",
            "service status",
            "what services",
            "systemd services",
        )
    )


def format_live_check_updates_response(
    operation_result: Day2OperationResult, *, result: ProvisioningJobResult
) -> str:
    evidence = dict(operation_result.evidence or {})
    hostname = result.instance_name or result.instance_id
    total = evidence.get("total_upgradable", 0)
    security = evidence.get("security_updates", 0)
    if operation_result.status != "completed":
        error_msg = (operation_result.error or {}).get("message", "unknown error")
        return (
            f"`{hostname}` — package update check failed.\n\n"
            f"- Error: `{error_msg}`\n\n"
            "Check that the AVA runner SSH key is present and the VM is reachable."
        )
    high_impact = list(evidence.get("high_impact") or [])
    reboot_required = bool(evidence.get("reboot_required", False))
    priority_line = ""
    if high_impact:
        names = ", ".join(f"`{p}`" for p in high_impact)
        priority_line = f"\n- Priority (high-impact): {names}"
    reboot_line = f"\n- Reboot required after patching: {'yes' if reboot_required else 'no'}"
    return (
        f"`{hostname}` — **{total}** package(s) upgradable "
        f"(**{security}** security).\n"
        f"{priority_line}"
        f"{reboot_line}\n\n"
        "Applying updates requires a separate approved patch operation (`patch_server`).\n"
        "No packages were modified by this check."
    )


def format_full_package_list(evidence: dict[str, Any], *, hostname: str) -> str:
    """Render the complete upgradable package list grouped into security / other."""
    security_pkgs = list(evidence.get("security_packages") or [])
    all_pkgs = list(evidence.get("packages") or [])
    security_set = set(security_pkgs)
    other_pkgs = [p for p in all_pkgs if p not in security_set]
    sec_lines = "\n".join(f"  - `{p}`" for p in security_pkgs) if security_pkgs else "  (none)"
    other_lines = "\n".join(f"  - `{p}`" for p in other_pkgs) if other_pkgs else "  (none)"
    return (
        f"`{hostname}` — full upgradable package list ({len(all_pkgs)} total).\n\n"
        f"**Security updates ({len(security_pkgs)}):**\n{sec_lines}\n\n"
        f"**Other updates ({len(other_pkgs)}):**\n{other_lines}\n\n"
        "No packages were modified by this check."
    )


def format_live_check_services_response(
    operation_result: Day2OperationResult, *, result: ProvisioningJobResult
) -> str:
    evidence = dict(operation_result.evidence or {})
    hostname = result.instance_name or result.instance_id
    if operation_result.status != "completed":
        error_msg = (operation_result.error or {}).get("message", "unknown error")
        return (
            f"`{hostname}` — service check failed.\n\n"
            f"- Error: `{error_msg}`\n\n"
            "Check that the AVA runner SSH key is present and the VM is reachable."
        )
    running = list(evidence.get("running") or [])
    failed = list(evidence.get("failed") or [])
    running_count = evidence.get("running_count", len(running))
    failed_count = evidence.get("failed_count", len(failed))
    running_names = ", ".join(f"`{s}`" for s in running[:12])
    if len(running) > 12:
        running_names += f", … (+{len(running) - 12} more)"
    failed_names = ", ".join(f"`{s}`" for s in failed) if failed else "none"
    return (
        f"`{hostname}` — service status (read-only snapshot).\n\n"
        f"- Running ({running_count}): {running_names or 'none'}\n"
        f"- Failed ({failed_count}): {failed_names}\n\n"
        "No service was started or stopped by this check."
    )


def format_live_check_updates_queued_response(
    operation_id: str, *, result: ProvisioningJobResult
) -> str:
    return (
        "Package update check has been queued for the Windows host runner.\n\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- Operation ID: `{operation_id}`\n"
        "- Status: `queued`\n\n"
        "Ask `show updates` again in a few seconds to see the results."
    )


def format_live_check_services_queued_response(
    operation_id: str, *, result: ProvisioningJobResult
) -> str:
    return (
        "Service status check has been queued for the Windows host runner.\n\n"
        f"- VM: `{result.instance_name or result.instance_id}`\n"
        f"- Operation ID: `{operation_id}`\n"
        "- Status: `queued`\n\n"
        "Ask `check services` again in a few seconds to see the results."
    )


def _format_check_updates_stored(*, session: Any, result: ProvisioningJobResult) -> str:
    return (
        f"Package update check for `{result.instance_name or result.instance_id}`.\n\n"
        "- Live runner check: `not yet executed`\n\n"
        "AVA will run `apt-get update` and `apt list --upgradable` on the VM via the Windows host runner. "
        "No stored update evidence exists yet. Ask `show updates` after the runner is online."
    )


def _format_check_services_stored(*, session: Any, result: ProvisioningJobResult) -> str:
    return (
        f"Service status check for `{result.instance_name or result.instance_id}`.\n\n"
        "- Live runner check: `not yet executed`\n\n"
        "AVA will run `systemctl list-units` on the VM via the Windows host runner. "
        "No stored service evidence exists yet. Ask `check services` after the runner is online."
    )


def _format_list_all_packages_stored(*, session: Any, result: ProvisioningJobResult) -> str:
    return (
        f"Full package list for `{result.instance_name or result.instance_id}`.\n\n"
        "- Live runner check: `not yet executed`\n\n"
        "No stored update scan exists yet. Ask `show updates` after the runner is online."
    )


def _human_action(operation: Day2Operation) -> str:
    labels = {
        "restart_nginx": "restart nginx",
        "snapshot": "take a VM snapshot",
        "rollback_snapshot": "roll back the VM to the latest AVA snapshot",
        "stop_vm": "stop the VM",
        "start_vm": "start the VM",
        "delete_vm": "delete the VM and its disk files",
    }
    return labels.get(operation.operation, operation.description)
