"""Serving-layer adapter for AVA v2 guided provisioning conversations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Any

from control import approval
from provisioning.conversation import ProvisioningFlowEngine, SessionManager, SessionPhase
from provisioning.day2 import (
    classify_day2_operation,
    format_approved_queued_response,
    format_approval_required_response,
    format_approved_pending_response,
    format_live_nginx_logs_queued_response,
    format_live_nginx_logs_response,
    format_live_verify_queued_response,
    format_live_verify_response,
    format_open_ssh_console_queued_response,
    format_open_ssh_console_response,
    format_read_only_response,
)
from provisioning.runner import ProvisioningJobQueue, ProvisioningJobResult, RedisProvisioningJobQueue


@dataclass(slots=True)
class ProvisioningServingResult:
    """User-facing response returned to AVA's `/ask` serving contract."""

    handled: bool
    response: str = ""
    confidence: str = "high"
    metadata: dict[str, Any] = field(default_factory=dict)


class ProvisioningChatService:
    """Bridge natural chat turns into the v2 provisioning FSM."""

    def __init__(self, db_path: str | Path, job_queue: ProvisioningJobQueue | None = None):
        self.sessions = SessionManager(db_path)
        self.flow = ProvisioningFlowEngine(self.sessions)
        self.job_queue = job_queue or RedisProvisioningJobQueue()

    def handle(self, user_id: str, query: str, *, route_intent: str | None = None) -> ProvisioningServingResult:
        user_id = str(user_id or "default")
        query = (query or "").strip()
        normalized = _normalize(query)
        is_provisioning_start = route_intent == "provisioning" or _is_provisioning_start_query(normalized)
        active = self._active_session(user_id)

        if active and _is_cancel(normalized):
            job_id = (active.collected_answers or {}).get("runner_job_id")
            if job_id:
                try:
                    self.job_queue.write_status(job_id, "cancelled")
                except Exception:
                    pass
            response = self.flow.cancel(active.session_id)
            return self._result(response.message, response.session)

        if _is_server_inventory_query(normalized):
            return ProvisioningServingResult(handled=True, response=self._server_inventory_response(user_id))

        if is_provisioning_start:
            if active:
                active_runner = self._runner_snapshot(active)
                if active.phase == SessionPhase.AWAITING_APPROVAL and (
                    self._known_vm_name_conflict(user_id, active)
                    or self._runner_vm_name_conflict(active)
                ):
                    self._mark_conflict_session_failed(active)
                    active = None
                elif _runner_failed(active_runner):
                    active = None
                elif _runner_completed(active_runner):
                    if active.phase != SessionPhase.COMPLETED:
                        self.sessions.save(active.with_updates(phase=SessionPhase.COMPLETED))
                    active = None
                else:
                    return self._result(_format_active_provisioning_guard(active, active_runner), active)
            existing = self._existing_managed_vm_session(user_id)
            if existing and not _is_explicit_additional_provisioning(normalized):
                return self._result(_format_existing_vm_guard(existing, self._runner_snapshot(existing)), existing)
            response = self.flow.start(user_id, query)
            return self._maybe_queue_approval(response)

        target_session = self._find_named_server_session(user_id, normalized)
        if target_session and _looks_like_server_management_query(normalized):
            active = target_session
        elif _mentions_ava_server_name(normalized) and _looks_like_server_management_query(normalized):
            return ProvisioningServingResult(
                handled=True,
                response=(
                    "I could not find that AVA-managed server in my current inventory.\n\n"
                    "Ask `list my servers` to see the server names I can manage, then retry with one of those names."
                ),
            )

        lifecycle_operation = classify_day2_operation(normalized)
        if lifecycle_operation and lifecycle_operation.operation in {"start_vm", "stop_vm", "delete_vm"} and not target_session:
            return ProvisioningServingResult(
                handled=True,
                response=_format_lifecycle_hostname_required(
                    lifecycle_operation.operation,
                    self._server_inventory_rows(user_id),
                ),
            )

        if not active:
            active = self._recent_session_for_followup(user_id, normalized)
            if not active:
                return ProvisioningServingResult(handled=False)

        runner = self._runner_snapshot(active)

        day2_approval = self._maybe_handle_day2_approval(normalized, active, runner)
        if day2_approval:
            return day2_approval

        day2 = self._maybe_handle_day2_operation(normalized, active, runner)
        if day2:
            return day2

        if _is_status_query(normalized):
            return self._result(_format_status_response(active, runner), active)

        if _is_connection_query(normalized):
            return self._result(_format_connection_response(active, runner), active)

        if _is_evidence_query(normalized):
            return self._result(_format_evidence_response(active, runner), active)

        if _is_web_verification_query(normalized):
            return self._result(_format_verification_response(active, runner), active)

        if _runner_completed(runner) and _is_first_login_confirmation(normalized):
            session = active
            if active.phase == SessionPhase.AWAITING_FIRST_LOGIN:
                session = self.flow.confirm_first_login(active.session_id).session
            return self._result(
                "First-login confirmation recorded. The host runner had already completed this VM, "
                "so there is no extra provisioning step waiting on this confirmation.\n\n"
                + _format_connection_response(session, runner),
                session,
            )

        completed_hardening_answers = _extract_post_login_answers(normalized)
        if _runner_completed(runner) and completed_hardening_answers and _looks_like_hardening_followup(normalized, active):
            session = self.sessions.record_answers(active.session_id, completed_hardening_answers)
            profile = completed_hardening_answers.get("hardening_profile", "baseline_linux")
            if profile == "none":
                message = (
                    "Hardening opt-out noted in the chat session, but the host runner had already "
                    "completed this VM. In this completed run, no new provisioning or rollback step is started."
                )
            else:
                message = (
                    "Already done. The host runner already applied `baseline_linux`, bootstrapped nginx, "
                    "and verified the web server with HTTP 200."
                )
            return self._result(
                message
                + "\n\n"
                + "\n".join(_format_hardening_summary(session, runner.get("result"))),
                session,
            )

        phase = active.phase
        if phase == SessionPhase.AWAITING_VM_TYPE:
            answers = _extract_role_answer(normalized)
            if answers.get("unsupported_role"):
                return self._result(
                    "For v2.0.0 I can build only one VM type: a VirtualBox Ubuntu web server. "
                    "Database, load balancer, and cache roles are intentionally deferred until after v2.0.0.",
                    active,
                )
            if not answers:
                return ProvisioningServingResult(handled=False)
            response = self.flow.submit_answers(active.session_id, answers)
            return self._maybe_queue_approval(response)

        if phase == SessionPhase.AWAITING_SPECS:
            answers = _extract_spec_answers(normalized)
            if not answers:
                return ProvisioningServingResult(handled=False)
            response = self.flow.submit_answers(active.session_id, answers)
            return self._maybe_queue_approval(response)

        if phase == SessionPhase.AWAITING_APPROVAL:
            approval_id = _extract_chat_approval_id(normalized)
            if approval_id:
                expected_id = active.approval_id
                if not expected_id:
                    return self._result(
                        "This provisioning session does not have an approval request yet.",
                        active,
                    )
                if approval_id != expected_id:
                    return self._result(
                        f"Approval ID `{approval_id}` does not match this provisioning request. "
                        f"Use `approve {expected_id}` to approve the active request.",
                        active,
                    )
                queue_entry = approval.get_by_id(approval_id)
                if queue_entry and queue_entry.get("status") == "approved":
                    return self._result(_format_approval_recorded_response(active), active, approval_id=approval_id)
                if not queue_entry or queue_entry.get("status") != "pending":
                    status = queue_entry.get("status") if queue_entry else "not_found"
                    return self._result(
                        f"Approval `{approval_id}` is not pending; current status is `{status}`.",
                        active,
                    )
                approval.update_status(approval_id, "approved")
                return self._result(_format_approval_recorded_response(active), active, approval_id=approval_id)
            if not _is_approval_continuation(normalized):
                return ProvisioningServingResult(handled=False)
            if not _runner_is_healthy(self.job_queue):
                return self._result(_format_runner_unavailable_response(), active)
            runner_conflict = self._runner_vm_name_conflict(active)
            if runner_conflict:
                active = self._mark_conflict_session_failed(active)
                return self._result(
                    _format_runner_vm_name_conflict(active, runner_conflict, approval_recorded=True),
                    active,
                )
            response = self.flow.continue_after_approval(active.session_id)
            message = self._format_approval_and_enqueue(response)
            return self._result(message, response.session, approval_id=response.approval_id)

        if phase == SessionPhase.AWAITING_FIRST_LOGIN:
            if not _is_first_login_confirmation(normalized):
                return ProvisioningServingResult(handled=False)
            response = self.flow.confirm_first_login(active.session_id)
            if _runner_completed(runner):
                return self._result(
                    response.message
                    + "\n\nRunner status is already completed, so the VM has already been created, "
                    "bootstrapped, hardened, and verified. No extra execution step is waiting on this "
                    "chat confirmation.\n\n"
                    + _format_connection_response(response.session, runner),
                    response.session,
                )
            return self._result(
                response.message
                + "\n\nHardening is default-on with `baseline_linux`. Reply `yes harden it` to keep hardening, "
                "or `skip hardening` if you explicitly want to opt out.",
                response.session,
            )

        if phase == SessionPhase.AWAITING_POST_LOGIN_CHOICES:
            answers = _extract_post_login_answers(normalized)
            if not answers:
                return ProvisioningServingResult(handled=False)
            self.sessions.record_answers(active.session_id, answers)
            if _runner_completed(runner):
                session = self.sessions.require(active.session_id)
                profile = answers.get("hardening_profile", "baseline_linux")
                if profile == "none":
                    message = (
                        "Hardening opt-out recorded in the chat session. The host runner had already "
                        "completed this VM, so no new execution step is being started."
                    )
                else:
                    message = (
                        "Baseline hardening recorded in the chat session. The host runner has already "
                        "applied `baseline_linux`, bootstrapped nginx, and verified the web server."
                    )
                return self._result(
                    message
                    + "\n\nAsk `show me the provisioning status`, `verify the web server`, or "
                    "`what did you do and what evidence do you have?` for the completion details.",
                    session,
                )
            session = self.sessions.update_phase(active.session_id, SessionPhase.BOOTSTRAPPING)
            if answers.get("hardening_profile") == "none":
                message = (
                    "Hardening opt-out recorded for this provisioning session. "
                    "The next execution step is web_server bootstrap and verification."
                )
            else:
                message = (
                    "Baseline hardening recorded for this provisioning session. "
                    "The next execution step is web_server bootstrap and verification."
                )
            return self._result(message, session)

        return ProvisioningServingResult(handled=False)

    def _active_session(self, user_id: str):
        sessions = self.sessions.list_active(user_id)
        return sessions[0] if sessions else None

    def _recent_session_for_followup(self, user_id: str, normalized: str):
        if not _is_provisioning_followup_query(normalized):
            return None
        recent_sessions = getattr(self.sessions, "list_recent", lambda *_args, **_kwargs: [])(user_id, limit=5)
        for session in recent_sessions:
            if session.instance_id or (session.collected_answers or {}).get("runner_job_id") or session.approval_id:
                return session
        return None

    def _server_inventory_rows(self, user_id: str) -> list[dict[str, Any]]:
        recent_sessions = getattr(self.sessions, "list_recent", lambda *_args, **_kwargs: [])(user_id, limit=25)
        inventory_snapshot = self._runner_inventory_snapshot()
        live_inventory = inventory_snapshot["entries"]
        template_name = inventory_snapshot.get("template_name") or ""
        managed_by_name: dict[str, dict[str, Any]] = {}
        for session in recent_sessions:
            runner = self._runner_snapshot(session)
            if not _runner_completed(runner):
                continue
            result = runner.get("result")
            if not (result and result.instance_id):
                continue
            vm_name = result.instance_name or result.instance_id
            if not vm_name:
                continue
            managed_by_name.setdefault(
                _normalize(vm_name),
                {
                    "session": session,
                    "runner": runner,
                    "result": result,
                    "phase": _effective_phase(session, runner),
                    "runner_status": runner.get("status") or "not available yet",
                },
            )

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for normalized_name, live_entry in sorted(live_inventory.items(), key=lambda item: str(item[1].get("name") or item[0])):
            vm_name = str(live_entry.get("name") or normalized_name).strip()
            if not vm_name or normalized_name in seen:
                continue
            seen.add(normalized_name)
            managed = managed_by_name.get(normalized_name, {})
            rows.append(
                {
                    "session": managed.get("session"),
                    "runner": managed.get("runner") or {},
                    "result": managed.get("result"),
                    "vm_name": vm_name,
                    "phase": managed.get("phase") or "external",
                    "runner_status": managed.get("runner_status") or "not tracked by AVA",
                    "inventory_present": bool(live_entry),
                    "ava_managed": bool(managed),
                    "runner_inventory_available": inventory_snapshot["available"],
                    "runner_inventory_stale": inventory_snapshot["stale"],
                    "runner_inventory_updated_at": inventory_snapshot["updated_at"],
                    "power_state": str(live_entry.get("power_state") or "not checked"),
                    "provider_status": str(live_entry.get("provider_status") or "not checked"),
                    "is_template": bool(template_name and _normalize(vm_name) == _normalize(template_name)),
                }
            )
        return rows

    def _runner_inventory_snapshot(self) -> dict[str, Any]:
        heartbeat_getter = getattr(self.job_queue, "get_runner_heartbeat", None)
        if heartbeat_getter is None:
            return {"available": False, "stale": True, "updated_at": None, "entries": {}}
        try:
            heartbeat = heartbeat_getter() or {}
        except Exception:
            return {"available": False, "stale": True, "updated_at": None, "entries": {}}
        metadata = heartbeat.get("metadata") if isinstance(heartbeat, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}

        updated_at = heartbeat.get("updated_at") if isinstance(heartbeat, dict) else None
        stale = False
        if updated_at:
            try:
                updated = datetime.fromisoformat(str(updated_at))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                stale = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds() > 120
            except ValueError:
                stale = True

        inventory: dict[str, dict[str, Any]] = {}
        entries = metadata.get("registered_vm_inventory")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                if not name:
                    continue
                inventory[_normalize(name)] = dict(entry)
        registered = metadata.get("registered_vms")
        if isinstance(registered, list):
            for vm_name in registered:
                name = str(vm_name or "").strip()
                if not name:
                    continue
                inventory.setdefault(
                    _normalize(name),
                    {
                        "name": name,
                        "power_state": "not checked",
                        "provider_status": "not checked",
                    },
                )
        return {
            "available": bool(heartbeat),
            "stale": stale,
            "updated_at": updated_at,
            "entries": inventory,
            "template_name": str(metadata.get("template_name") or ""),
        }

    def _runner_virtualbox_inventory(self) -> dict[str, dict[str, Any]]:
        return self._runner_inventory_snapshot()["entries"]

    def _server_inventory_response(self, user_id: str) -> str:
        inventory_snapshot = self._runner_inventory_snapshot()
        rows = self._server_inventory_rows(user_id)
        if not rows:
            if not inventory_snapshot["available"]:
                return (
                    "I cannot confirm the current VirtualBox server inventory because the AVA host runner "
                    "is not reporting inventory yet.\n\n"
                    "Start AVA normally with `scripts/start-ava.ps1`, then ask `list my servers` again."
                )
            return (
                "The AVA host runner is online, but VirtualBox is not reporting any servers right now.\n\n"
                "Create a web server first, then ask `list my servers` again."
            )
        lines = ["VirtualBox servers reported by the AVA host runner:\n"]
        if inventory_snapshot.get("updated_at"):
            freshness = "stale" if inventory_snapshot.get("stale") else "fresh"
            lines.extend([f"- Inventory updated: `{inventory_snapshot['updated_at']}` (`{freshness}`)", ""])
        for index, row in enumerate(rows, start=1):
            result = row.get("result")
            web_url = f"http://127.0.0.1:{result.http_port}/" if result and result.http_port else "not known to AVA"
            ssh = f"{result.ssh_host}:{result.ssh_port}" if result and result.ssh_host and result.ssh_port else "not known to AVA"
            if row.get("is_template"):
                managed_text = "Role: template (protected clone source)"
            elif row.get("ava_managed"):
                managed_text = "yes"
            else:
                managed_text = "no - visible in VirtualBox only"
            lines.extend(
                [
                    f"{index}. `{row['vm_name']}`",
                    f"- Managed by AVA: `{managed_text}`",
                    f"- Phase: `{row['phase']}`",
                    f"- Runner status: `{row['runner_status']}`",
                    f"- Power state: `{row['power_state']}`",
                    f"- Provider status: `{row['provider_status']}`",
                    f"- SSH / PuTTY: `{ssh}`",
                    f"- Web URL: `{web_url}`",
                    "",
                ]
            )
            if row.get("is_template"):
                lines.extend(
                    [
                        "  Protected: this is the provisioning template — keep it powered off.",
                        "",
                    ]
                )
            elif not row.get("ava_managed"):
                lines.extend(
                    [
                        "  AVA can see this VM in VirtualBox, but it does not have provisioning credentials or web evidence for it yet.",
                        "",
                    ]
                )
            if not row.get("is_template") and _power_state_needs_operator_choice(row["power_state"]):
                lines.extend(
                    [
                        f"  `{row['vm_name']}` is not running. Say `start {row['vm_name']}` to power it on, "
                        f"or `delete {row['vm_name']}` to remove it after approval.",
                        "",
                    ]
                )
        lines.extend(
            [
                "Target examples:",
                "- `verify ava-web-03`",
                "- `show nginx logs for ava-web-03`",
                "- `open web console for ava-web-03`",
                "- `restart nginx on ava-web-03`",
                "- `stop ava-web-03`",
                "- `start ava-web-03`",
                "- `delete ava-web-03`",
            ]
        )
        return "\n".join(lines).strip()

    def _find_named_server_session(self, user_id: str, normalized: str):
        if not normalized:
            return None
        rows = self._server_inventory_rows(user_id)
        # Prefer the longest VM name first so ava-web-03 wins before ava-web.
        rows.sort(key=lambda row: len(str(row["vm_name"])), reverse=True)
        for row in rows:
            if not (row.get("session") and row.get("result")):
                continue
            names = _server_name_candidates(row["session"], row["runner"], row["result"])
            if any(name and name in normalized for name in names):
                return row["session"]
        return None

    def _existing_managed_vm_session(self, user_id: str):
        recent_sessions = getattr(self.sessions, "list_recent", lambda *_args, **_kwargs: [])(user_id, limit=10)
        for session in recent_sessions:
            runner = self._runner_snapshot(session)
            result = runner.get("result")
            if result and result.instance_id and not result.error and self._existing_vm_still_live(session, runner, result):
                return session
        return None

    def _known_vm_name_conflict(self, user_id: str, session) -> dict[str, Any] | None:
        desired_state = session.desired_state or {}
        requested_name = _normalize(str(desired_state.get("vm_name") or desired_state.get("hostname") or ""))
        if not requested_name:
            return None
        inventory_snapshot = self._runner_inventory_snapshot()
        for row in self._server_inventory_rows(user_id):
            row_session = row.get("session")
            if row_session and row_session.session_id == session.session_id:
                continue
            names = {_normalize(str(row.get("vm_name") or ""))}
            if row_session and row.get("result"):
                names.update(_server_name_candidates(row_session, row.get("runner") or {}, row.get("result")))
            if requested_name in names:
                return row
        if inventory_snapshot["available"]:
            return None
        recent_sessions = getattr(self.sessions, "list_recent", lambda *_args, **_kwargs: [])(user_id, limit=25)
        for known_session in recent_sessions:
            if known_session.session_id == session.session_id:
                continue
            runner = self._runner_snapshot(known_session)
            if not _runner_completed(runner):
                continue
            result = runner.get("result")
            if not (result and result.instance_id):
                continue
            names = _server_name_candidates(known_session, runner, result)
            if requested_name in names:
                vm_name = result.instance_name or result.instance_id
                return {
                    "session": known_session,
                    "runner": runner,
                    "result": result,
                    "vm_name": vm_name,
                    "phase": _effective_phase(known_session, runner),
                    "runner_status": runner.get("status") or "not available yet",
                    "inventory_present": False,
                    "ava_managed": True,
                    "power_state": "not checked",
                    "provider_status": "not checked",
                }
        return None

    def _runner_vm_name_conflict(self, session) -> dict[str, Any] | None:
        desired_state = session.desired_state or {}
        requested_raw = str(desired_state.get("vm_name") or desired_state.get("hostname") or "").strip()
        requested_name = _normalize(requested_raw)
        if not requested_name:
            return None
        for normalized_name, entry in self._runner_virtualbox_inventory().items():
            if normalized_name == requested_name:
                return {
                    "vm_name": str(entry.get("name") or requested_raw),
                    "requested_name": requested_raw or str(entry.get("name") or requested_name),
                    "source": "virtualbox_runner_inventory",
                    "power_state": str(entry.get("power_state") or "not checked"),
                    "provider_status": str(entry.get("provider_status") or "not checked"),
                }
        return None

    def _existing_vm_still_live(self, session, runner: dict[str, Any], result: ProvisioningJobResult) -> bool:
        """Use live runner truth before letting an old completed VM block new provisioning."""
        if not getattr(self.job_queue, "is_runner_healthy", lambda: False)():
            return False
        enqueue_operation = getattr(self.job_queue, "enqueue_day2_operation", None)
        if enqueue_operation is None:
            return False
        try:
            day2_job = enqueue_operation(
                session_id=session.session_id,
                operation="verify",
                target="web_server",
                instance_id=result.instance_id,
                instance_name=result.instance_name,
                ssh_host=result.ssh_host,
                ssh_port=result.ssh_port,
                http_port=result.http_port,
                metadata={
                    "read_only": True,
                    "guard_check": True,
                    "runner_job_id": runner.get("job_id"),
                    "username": (session.collected_answers or {}).get("username", "avaadmin"),
                },
            )
        except Exception:
            return False
        operation_result = self._wait_for_day2_result(day2_job.operation_id, timeout_seconds=6)
        if operation_result is None:
            return False
        existing_operations = list((session.collected_answers or {}).get("day2_operation_ids") or [])
        existing_operations.append(day2_job.operation_id)
        self.sessions.record_answers(
            session.session_id,
            {
                "last_day2_operation_id": day2_job.operation_id,
                "day2_operation_ids": existing_operations[-10:],
            },
        )
        if getattr(operation_result, "status", None) != "completed" or getattr(operation_result, "error", None):
            return False
        checks = ((getattr(operation_result, "evidence", None) or {}).get("checks") or [])
        required = {"vm_exists", "vm_running"}
        passed = {check.get("name") for check in checks if check.get("passed") is True}
        return required.issubset(passed)

    def _maybe_queue_approval(self, response):
        if response.requires_approval and response.desired_state_ready:
            conflict = self._known_vm_name_conflict(response.session.user_id, response.session)
            if conflict:
                session = self._mark_conflict_session_failed(response.session)
                return self._result(_format_known_vm_name_conflict(session, conflict), session)
            runner_conflict = self._runner_vm_name_conflict(response.session)
            if runner_conflict:
                session = self._mark_conflict_session_failed(response.session)
                return self._result(_format_runner_vm_name_conflict(session, runner_conflict), session)
            response = self.flow.request_approval(response.session.session_id)
        return self._result(_format_flow_response(response), response.session, approval_id=response.approval_id)

    def _mark_conflict_session_failed(self, session):
        if session.phase == SessionPhase.FAILED:
            return session
        return self.sessions.update_phase(session.session_id, SessionPhase.FAILED)

    def _format_approval_and_enqueue(self, response) -> str:
        message = _format_flow_response(response)
        credential = response.credential
        if not credential:
            return message
        try:
            job = self.job_queue.enqueue_approved_job(
                session_id=response.session.session_id,
                desired_state=response.session.desired_state,
                credential_id=credential.credential_id,
                username=credential.username,
                temporary_password=credential.temporary_password or "",
            )
            session = self.sessions.record_answers(
                response.session.session_id,
                {
                    "runner_job_id": job.job_id,
                    "username": credential.username,
                    "credential_displayed_once": True,
                },
            )
            response.session = session
            return (
                message
                + "\n\nVM provisioning is now in process.\n"
                + "AVA has queued the Windows host runner and will track each stage in `provisioning status`.\n\n"
                + "Runner job queued for host-side VirtualBox execution.\n"
                + f"Job ID: `{job.job_id}`\n"
                + "Expected time: usually 3-8 minutes, depending on VirtualBox boot and Ubuntu cloud-init.\n"
                + "AVA will report the hostname, SSH/PuTTY port, and web URL after bootstrap, hardening, "
                + "and HTTP verification complete. After that, type `open web console` to connect."
            )
        except Exception as exc:
            session = self.sessions.record_answers(
                response.session.session_id,
                {"runner_enqueue_error": str(exc)},
            )
            response.session = session
            return (
                message
                + "\n\nRunner job could not be queued, so no VM will be created yet.\n"
                + f"Queue error: `{exc}`"
            )

    def _runner_snapshot(self, session) -> dict[str, Any]:
        job_id = (session.collected_answers or {}).get("runner_job_id")
        answers = session.collected_answers or {}
        day2_operation_id = answers.get("last_day2_operation_id")
        day2_status = None
        day2_result = None
        if day2_operation_id and hasattr(self.job_queue, "get_day2_status"):
            day2_status = self.job_queue.get_day2_status(day2_operation_id)
            day2_result = self.job_queue.get_day2_result(day2_operation_id)
        if not job_id:
            return {
                "job_id": None,
                "status": None,
                "result": None,
                "day2_operation_id": day2_operation_id,
                "day2_status": day2_status,
                "day2_result": day2_result,
            }
        # I5: defensive reads — ConnectionError / expired key degrades to None, never raises.
        try:
            status = self.job_queue.get_status(job_id)
        except Exception:
            status = None
        try:
            result = self.job_queue.get_result(job_id)
        except Exception:
            result = None
        get_progress = getattr(self.job_queue, "get_progress", None)
        try:
            progress = get_progress(job_id) if get_progress else None
        except Exception:
            progress = None
        if progress and not status:
            status = getattr(progress, "status", None)
        if progress and getattr(progress, "instance_id", None) and not session.instance_id:
            session = self.sessions.save(session.with_updates(instance_id=progress.instance_id))
        # I1: error is the authoritative terminal signal and is evaluated BEFORE instance_id.
        # A result with both instance_id and error set (instance_id identifies the rolled-back
        # VM for tracing) is FAILED — instance_id alone must never mask a failure.
        if result and result.error:
            status = "failed"
            # I2 + I3: write terminal phase AND identity in one save so SQLite is always
            # consistent with the Redis result. instance_id is recorded as identity even on
            # failure so AVA can say "VM X was created then rolled back".
            _updates: dict[str, Any] = {}
            if result.instance_id and session.instance_id != result.instance_id:
                _updates["instance_id"] = result.instance_id
            if session.phase != SessionPhase.FAILED:
                _updates["phase"] = SessionPhase.FAILED
            if _updates:
                session = self.sessions.save(session.with_updates(**_updates))
        elif result and result.instance_id:
            status = "completed"
            # I2 + I3: write COMPLETED and identity for successful results too.
            _updates = {}
            if session.instance_id != result.instance_id:
                _updates["instance_id"] = result.instance_id
            if session.phase not in {SessionPhase.COMPLETED, SessionPhase.FAILED, SessionPhase.CANCELLED}:
                _updates["phase"] = SessionPhase.COMPLETED
            if _updates:
                session = self.sessions.save(session.with_updates(**_updates))
        elif _runner_job_orphaned(session, status, self.job_queue):
            status = "failed"
            result = ProvisioningJobResult(
                job_id=job_id,
                instance_id=session.instance_id,
                instance_name=session.instance_id,
                verification_evidence={},
                completion_timestamp=_utc_now(),
                error={
                    "failed_step": "host_runner",
                    "failure_class": "runner_orphaned",
                    "message": (
                        "The Windows host runner stopped before AVA received a final provisioning result. "
                        "The previous attempt is no longer safe to track; start AVA normally and retry."
                    ),
                },
            )
            if session.phase != SessionPhase.FAILED:
                session = self.sessions.save(session.with_updates(phase=SessionPhase.FAILED))
        elif status is None and _runner_job_state_expired(session):
            status = "failed"
            result = ProvisioningJobResult(
                job_id=job_id,
                instance_id=session.instance_id,
                instance_name=session.instance_id,
                verification_evidence={},
                completion_timestamp=_utc_now(),
                error={
                    "failed_step": "host_runner",
                    "failure_class": "runner_state_expired",
                    "message": (
                        "Runner job state expired before AVA observed a terminal result. "
                        "The previous provisioning attempt is stale; start a fresh request."
                    ),
                },
            )
            if session.phase != SessionPhase.FAILED:
                session = self.sessions.save(session.with_updates(phase=SessionPhase.FAILED))
        # I2 catch-all: if status resolved to a terminal value through any path but the
        # session phase wasn't written above (e.g. status-only "failed" key, no result),
        # write through now so no session stays non-terminal with a terminal job status.
        if status in {"failed", "completed"} and session.phase not in {
            SessionPhase.COMPLETED, SessionPhase.FAILED, SessionPhase.CANCELLED
        }:
            target = SessionPhase.FAILED if status == "failed" else SessionPhase.COMPLETED
            session = self.sessions.save(session.with_updates(phase=target))
        return {
            "job_id": job_id,
            "status": status,
            "result": result,
            "progress": progress,
            "day2_operation_id": day2_operation_id,
            "day2_status": day2_status,
            "day2_result": day2_result,
        }

    def _maybe_handle_day2_operation(
        self,
        normalized: str,
        session,
        runner: dict[str, Any],
    ) -> ProvisioningServingResult | None:
        operation = classify_day2_operation(normalized)
        if not operation:
            return None
        if operation.operation == "status":
            return None
        completed_result = runner.get("result")
        result = completed_result or _runner_connection_result(runner)
        if not result or not result.instance_id:
            if operation.operation in {"status", "verify"}:
                return None
            return self._result(
                "I can manage an AVA-created VM after it has completed provisioning, but this chat "
                "session does not have a completed VM attached yet.\n\n"
                "Create and verify a VM first, then ask again.",
                session,
            )
        if not operation.requires_approval:
            if operation.operation in {"verify", "nginx_logs", "open_ssh_console"}:
                live_operation = self._queue_live_read_only_operation(operation, session, runner, result)
                if live_operation:
                    return live_operation
            return self._result(format_read_only_response(operation, session=session, result=result), session)

        if not completed_result:
            return self._result(
                "This VM is visible to AVA, but final provisioning has not completed yet.\n\n"
                "Mutating operations like restart, stop, start, snapshot, rollback, or delete are enabled only "
                "after AVA has completed bootstrap, hardening, and HTTP verification.",
                session,
            )

        _template_name = self._runner_inventory_snapshot().get("template_name") or ""
        _instance_name = str(result.instance_name or "")
        if _template_name and _normalize(_instance_name) == _normalize(_template_name):
            if operation.operation == "delete_vm":
                return self._result(
                    f"Refused: `{_instance_name}` is the provisioning template (clone source). "
                    "Deleting it would break all future VM creation.",
                    session,
                )

        approval_id = approval.add_request(
            f"day2:{operation.operation}:{result.instance_name or result.instance_id}",
            normalized,
            risk=operation.risk,
            mode="day2_operation",
            approval_key=f"day2:{operation.operation}:{result.instance_name or result.instance_id}",
            metadata={
                "type": "day2_operation",
                "operation": operation.operation,
                "target": operation.target,
                "session_id": session.session_id,
                "runner_job_id": runner.get("job_id"),
                "instance_id": result.instance_id,
                "instance_name": result.instance_name,
                "ssh_host": result.ssh_host,
                "ssh_port": result.ssh_port,
                "http_port": result.http_port,
            },
        )
        response_text = format_approval_required_response(operation, session=session, result=result, approval_id=approval_id)
        if _template_name and _normalize(_instance_name) == _normalize(_template_name) and operation.operation == "start_vm":
            response_text += (
                f"\n\n**Warning:** `{_instance_name}` is the provisioning template. "
                "Starting it locks the disk and blocks new clones until it is powered off again."
            )
        return self._result(response_text, session, approval_id=approval_id)

    def _maybe_handle_day2_approval(
        self,
        normalized: str,
        session,
        runner: dict[str, Any],
    ) -> ProvisioningServingResult | None:
        approval_id = _extract_chat_approval_id(normalized)
        if not approval_id:
            return None
        entry = approval.get_by_id(approval_id)
        metadata = dict((entry or {}).get("metadata") or {})
        if metadata.get("type") != "day2_operation":
            return None
        if entry.get("status") != "pending":
            return self._result(
                f"Approval `{approval_id}` is not pending; current status is `{entry.get('status')}`.",
                session,
                approval_id=approval_id,
            )
        target_session = self.sessions.get(str(metadata.get("session_id") or "")) or session
        target_runner = self._runner_snapshot(target_session)
        result = target_runner.get("result")
        if not result or not result.instance_id:
            return self._result(
                "This approval belongs to an AVA-managed VM, but the completed runner result is not "
                "attached to the active chat session right now. No action was executed.",
                target_session,
                approval_id=approval_id,
            )
        operation = classify_day2_operation(str(entry.get("query") or entry.get("command") or ""))
        if not operation:
            return self._result(
                f"Approval `{approval_id}` could not be matched to a supported VM operation. "
                "No action was executed.",
                target_session,
                approval_id=approval_id,
            )
        approval.update_status(approval_id, "approved")
        enqueue_operation = getattr(self.job_queue, "enqueue_day2_operation", None)
        if enqueue_operation is not None:
            try:
                day2_job = enqueue_operation(
                    session_id=target_session.session_id,
                    operation=operation.operation,
                    target=operation.target,
                    instance_id=result.instance_id,
                    instance_name=result.instance_name,
                    ssh_host=result.ssh_host,
                    ssh_port=result.ssh_port,
                    http_port=result.http_port,
                    metadata={"approval_id": approval_id, "runner_job_id": target_runner.get("job_id")},
                )
                existing_operations = list((target_session.collected_answers or {}).get("day2_operation_ids") or [])
                existing_operations.append(day2_job.operation_id)
                session = self.sessions.record_answers(
                    target_session.session_id,
                    {
                        "last_day2_operation_id": day2_job.operation_id,
                        "day2_operation_ids": existing_operations[-10:],
                    },
                )
                return self._result(
                    format_approved_queued_response(
                        operation,
                        session=session,
                        result=result,
                        operation_id=day2_job.operation_id,
                    ),
                    session,
                    approval_id=approval_id,
                )
            except Exception as exc:
                return self._result(
                    "Approval recorded, but AVA could not queue the server-management operation yet.\n\n"
                    f"- Operation: `{operation.operation}`\n"
                    f"- VM: `{result.instance_name or result.instance_id}`\n"
                    f"- Queue error: `{exc}`\n\n"
                    "No VM or service change was executed.",
                    target_session,
                    approval_id=approval_id,
                )
        return self._result(
            format_approved_pending_response(operation, session=target_session, result=result),
            target_session,
            approval_id=approval_id,
        )

    def _queue_live_read_only_operation(
        self,
        operation,
        session,
        runner: dict[str, Any],
        result: ProvisioningJobResult,
    ) -> ProvisioningServingResult | None:
        if not getattr(self.job_queue, "is_runner_healthy", lambda: False)():
            if operation.operation == "verify":
                return self._result(
                    "Live web-server verification needs the Windows host runner online.\n\n"
                    "- Live status: `not checked`\n"
                    "- Reason: `host runner is not reporting healthy`\n\n"
                    "Stored provisioning evidence may still exist, but AVA will not use stored history "
                    "as proof that the VM is currently running.",
                    session,
                )
            return self._result(
                "Live server-management evidence needs the Windows host runner online. "
                "The details below are last-known stored history, not a fresh live check.\n\n"
                + format_read_only_response(operation, session=session, result=result),
                session,
            )
        enqueue_operation = getattr(self.job_queue, "enqueue_day2_operation", None)
        if enqueue_operation is None:
            return None
        day2_job = enqueue_operation(
            session_id=session.session_id,
            operation=operation.operation,
            target=operation.target,
            instance_id=result.instance_id,
            instance_name=result.instance_name,
            ssh_host=result.ssh_host,
            ssh_port=result.ssh_port,
            http_port=result.http_port,
            metadata={
                "read_only": True,
                "runner_job_id": runner.get("job_id"),
                "username": (session.collected_answers or {}).get("username", "avaadmin"),
            },
        )
        existing_operations = list((session.collected_answers or {}).get("day2_operation_ids") or [])
        existing_operations.append(day2_job.operation_id)
        session = self.sessions.record_answers(
            session.session_id,
            {
                "last_day2_operation_id": day2_job.operation_id,
                "day2_operation_ids": existing_operations[-10:],
            },
        )
        operation_result = self._wait_for_day2_result(day2_job.operation_id, timeout_seconds=18)
        if operation_result:
            if operation.operation == "nginx_logs":
                return self._result(format_live_nginx_logs_response(operation_result, result=result), session)
            if operation.operation == "open_ssh_console":
                return self._result(format_open_ssh_console_response(operation_result, result=result), session)
            return self._result(format_live_verify_response(operation_result, result=result), session)
        if operation.operation == "nginx_logs":
            return self._result(format_live_nginx_logs_queued_response(day2_job.operation_id, result=result), session)
        if operation.operation == "open_ssh_console":
            return self._result(format_open_ssh_console_queued_response(day2_job.operation_id, result=result), session)
        return self._result(format_live_verify_queued_response(day2_job.operation_id, result=result), session)

    def _wait_for_day2_result(self, operation_id: str, *, timeout_seconds: float) -> Any | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = self.job_queue.get_day2_result(operation_id)
            if result is not None:
                return result
            status = self.job_queue.get_day2_status(operation_id)
            if status in {"failed", "cancelled"}:
                return self.job_queue.get_day2_result(operation_id)
            time.sleep(0.5)
        return None

    def _result(self, message: str, session, *, approval_id: str | None = None) -> ProvisioningServingResult:
        metadata = {
            "provisioning": {
                "session_id": session.session_id,
                "phase": session.phase.value,
                "role": session.role,
                "provider": session.provider,
                "approval_id": approval_id or session.approval_id,
                "desired_state": session.desired_state,
            }
        }
        return ProvisioningServingResult(handled=True, response=message, metadata=metadata)


def _normalize(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").lower()).strip(" ?!.")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _runner_completed(runner: dict[str, Any] | None) -> bool:
    if not runner:
        return False
    result = runner.get("result")
    return runner.get("status") == "completed" and bool(result and result.instance_id)


def _runner_progress_result(runner: dict[str, Any] | None) -> ProvisioningJobResult | None:
    runner = runner or {}
    progress = runner.get("progress")
    if not progress or not getattr(progress, "instance_id", None):
        return None
    return ProvisioningJobResult(
        job_id=getattr(progress, "job_id", runner.get("job_id") or ""),
        instance_id=getattr(progress, "instance_id", None),
        instance_name=getattr(progress, "instance_name", None) or getattr(progress, "instance_id", None),
        ssh_host=getattr(progress, "ssh_host", None),
        ssh_port=getattr(progress, "ssh_port", None),
        http_port=getattr(progress, "http_port", None),
        verification_evidence={"runner_progress": progress.to_dict() if hasattr(progress, "to_dict") else {}},
        completion_timestamp=getattr(progress, "updated_at", None) or _utc_now(),
        error=getattr(progress, "error", None),
    )


def _runner_connection_result(runner: dict[str, Any] | None) -> ProvisioningJobResult | None:
    runner = runner or {}
    result = runner.get("result")
    if result and result.instance_id:
        return result
    return _runner_progress_result(runner)


def _runner_progress_lines(runner: dict[str, Any] | None) -> list[str]:
    runner = runner or {}
    progress = runner.get("progress")
    if not progress:
        return []
    return [
        "Runner progress:",
        f"- Stage: `{getattr(progress, 'stage', 'unknown')}`",
        f"- Status: `{getattr(progress, 'status', 'unknown')}`",
        f"- VM: `{getattr(progress, 'instance_name', None) or getattr(progress, 'instance_id', None) or 'not created yet'}`",
        f"- Message: `{getattr(progress, 'message', None) or 'runner is working'}`",
        f"- Updated: `{getattr(progress, 'updated_at', 'unknown')}`",
    ]


def _runner_progress_guidance_lines(runner: dict[str, Any] | None) -> list[str]:
    runner = runner or {}
    progress = runner.get("progress")
    status = runner.get("status") or "queued"
    stage = getattr(progress, "stage", None) or status
    stage_key = str(stage or "queued").lower()
    guidance = {
        "queued": ("Waiting for the Windows host runner to pick up the job.", "usually under 1 minute after the runner is healthy"),
        "picked_up": ("Runner picked up the request and is preparing VirtualBox.", "usually 3-8 minutes total"),
        "provisioning": ("Creating and booting the VM.", "usually 3-8 minutes total"),
        "vm_started": ("VM has started; waiting for SSH to become reachable.", "usually 1-3 minutes"),
        "ssh_ready": ("SSH is reachable; waiting for cloud-init and first boot setup to finish.", "usually 2-6 minutes"),
        "cloud_init": ("Cloud-init is finishing OS setup.", "usually 2-6 minutes"),
        "bootstrapping": ("Installing and configuring the web-server role.", "usually 1-4 minutes"),
        "hardening": ("Applying baseline Linux and web-server hardening.", "usually 1-3 minutes"),
        "verifying": ("Verifying SSH, nginx, and HTTP access.", "usually under 1 minute"),
    }
    current_step, estimate = guidance.get(
        stage_key,
        ("Runner is still working on this provisioning job.", "usually 3-8 minutes total"),
    )
    return [
        "Provisioning guidance:",
        f"- Current step: `{current_step}`",
        f"- Estimated remaining time: `{estimate}`",
        "- Web Console: `available after Phase is completed and live verification has passed`",
        "- Next check: ask `provisioning status` again; when complete, type `open web console`.",
    ]


def _runner_failed(runner: dict[str, Any] | None) -> bool:
    if not runner:
        return False
    result = runner.get("result")
    return runner.get("status") == "failed" or bool(result and result.error)


def _is_vm_name_conflict_error(error: dict[str, Any] | None) -> bool:
    if not isinstance(error, dict):
        return False
    failure_class = str(error.get("failure_class") or "").lower()
    message = str(error.get("message") or "").lower()
    return failure_class == "vm_name_conflict" or ("virtualbox vm" in message and "already exists" in message)


def _runner_job_state_expired(session) -> bool:
    if session.phase in {SessionPhase.COMPLETED, SessionPhase.FAILED, SessionPhase.CANCELLED}:
        return False
    updated_at = getattr(session, "updated_at", None)
    if not updated_at:
        return False
    try:
        updated = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
    return age_seconds > 30 * 60


def _runner_job_orphaned(session, status: str | None, job_queue: ProvisioningJobQueue) -> bool:
    if session.phase in {SessionPhase.COMPLETED, SessionPhase.FAILED, SessionPhase.CANCELLED}:
        return False
    # I6: status=None means Redis was wiped (e.g. after a reboot) — treat the same as an
    # in-flight status so the offline-runner + elapsed-age check below can fire.
    if status is not None and status not in {"queued", "picked_up", "provisioning", "bootstrapping", "hardening", "verifying"}:
        return False
    if _runner_is_healthy(job_queue):
        return False
    updated_at = getattr(session, "updated_at", None)
    if not updated_at:
        return False
    try:
        updated = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
    return age_seconds > 2 * 60


def _runner_is_healthy(job_queue: ProvisioningJobQueue) -> bool:
    health_check = getattr(job_queue, "is_runner_healthy", None)
    if health_check is None:
        return True
    try:
        return bool(health_check())
    except Exception:
        return False


def _is_provisioning_followup_query(query: str) -> bool:
    if _extract_chat_approval_id(query):
        return True
    if _is_status_query(query) or _is_connection_query(query) or _is_evidence_query(query) or _is_web_verification_query(query):
        return True
    if _is_first_login_confirmation(query) or _extract_post_login_answers(query):
        return True
    return classify_day2_operation(query) is not None


def _is_server_inventory_query(query: str) -> bool:
    return any(
        marker in query
        for marker in (
            "list my servers",
            "show my servers",
            "list servers",
            "show servers",
            "server inventory",
            "vm inventory",
            "virtualbox inventory",
            "managed servers",
            "ava managed servers",
            "what servers do i have",
            "which servers do i have",
            "offline servers",
            "which servers are offline",
            "show offline servers",
            "what is offline",
            "powered off servers",
            "saved servers",
        )
    )


def _looks_like_server_management_query(query: str) -> bool:
    if classify_day2_operation(query) is not None:
        return True
    return (
        _is_status_query(query)
        or _is_connection_query(query)
        or _is_evidence_query(query)
        or _is_web_verification_query(query)
    )


def _mentions_ava_server_name(query: str) -> bool:
    return bool(re.search(r"\bava-[a-z0-9][a-z0-9-]{1,60}\b", query or ""))


def _server_name_candidates(session, runner: dict[str, Any] | None, result: ProvisioningJobResult | None = None) -> list[str]:
    runner = runner or {}
    result = result or _runner_connection_result(runner)
    candidates: list[str] = []
    if result:
        candidates.extend([result.instance_name or "", result.instance_id or ""])
    progress = runner.get("progress")
    if progress:
        candidates.extend(
            [
                str(getattr(progress, "instance_name", "") or ""),
                str(getattr(progress, "instance_id", "") or ""),
            ]
        )
    desired_state = session.desired_state or {}
    answers = session.collected_answers or {}
    candidates.extend(
        [
            session.instance_id or "",
            str(desired_state.get("vm_name") or ""),
            str(desired_state.get("hostname") or ""),
            str(answers.get("hostname") or ""),
        ]
    )
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = _normalize(str(candidate))
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def _is_explicit_additional_provisioning(query: str) -> bool:
    additional_markers = (
        "another web server",
        "additional web server",
        "second web server",
        "new web server",
        "one more web server",
        "create another",
        "create new",
        "provision another",
        "provision new",
    )
    return any(marker in query for marker in additional_markers)


def _is_provisioning_start_query(query: str) -> bool:
    if not query:
        return False
    if _is_explicit_additional_provisioning(query):
        return True
    role_markers = ("web server", "nginx server", "ubuntu web")
    action_markers = (
        "i want",
        "i need",
        "create",
        "provision",
        "build",
        "deploy",
        "make",
        "spin up",
        "set up",
        "setup",
        "launch",
    )
    return any(role in query for role in role_markers) and any(action in query for action in action_markers)


def _format_active_provisioning_guard(session, runner: dict[str, Any]) -> str:
    progress_result = _runner_progress_result(runner)
    progress_lines = _runner_progress_lines(runner)
    progress_block = ""
    if progress_lines:
        progress_block = "\n\n" + "\n".join(progress_lines)
        if progress_result and progress_result.ssh_host and progress_result.ssh_port:
            progress_block += f"\n- SSH / PuTTY: `{progress_result.ssh_host}:{progress_result.ssh_port}`"
        progress_block += "\n\n" + "\n".join(_runner_progress_guidance_lines(runner))
    return (
        "A provisioning request is already active, so I will not start another one on top of it.\n\n"
        f"- Current session: `{session.session_id}`\n"
        f"- Phase: `{_effective_phase(session, runner)}`\n"
        f"- Runner job ID: `{runner.get('job_id') or 'not queued yet'}`\n"
        f"- Runner status: `{runner.get('status') or 'not available yet'}`"
        f"{progress_block}\n\n"
        "Ask `show me the provisioning status` to continue tracking it, or say `cancel provisioning` "
        "if you want to stop this request before starting a different one."
    )


def _format_existing_vm_guard(session, runner: dict[str, Any]) -> str:
    result = runner.get("result")
    vm_name = (
        (result.instance_name if result else None)
        or session.instance_id
        or (result.instance_id if result else None)
        or "the existing AVA-managed VM"
    )
    ssh = ""
    web = ""
    if result and result.ssh_host and result.ssh_port:
        ssh = f"\n- SSH / PuTTY: `{result.ssh_host}:{result.ssh_port}`"
    if result and result.http_port:
        web = f"\n- Web URL: `http://127.0.0.1:{result.http_port}/`"
    return (
        "You already have an AVA-managed web server, so I will not create a second one by accident.\n\n"
        f"- Existing VM: `{vm_name}`"
        f"{ssh}"
        f"{web}\n\n"
        "Ask `show status of my web server`, `verify the web server`, or `show nginx logs` for this server.\n"
        "If you intentionally want a second server, say `create another web server` and include the specs."
    )


def _power_state_needs_operator_choice(power_state: str | None) -> bool:
    normalized = _normalize(str(power_state or ""))
    return normalized in {
        "aborted",
        "paused",
        "poweroff",
        "powered off",
        "saved",
        "stopped",
    }


def _format_lifecycle_hostname_required(operation: str, rows: list[dict[str, Any]]) -> str:
    verb = {
        "start_vm": "start",
        "stop_vm": "stop",
        "delete_vm": "delete",
    }.get(operation, "manage")
    if not rows:
        return (
            f"Which server should I {verb}?\n\n"
            "I do not have a completed AVA-managed server attached to this account yet. "
            "Ask `list my servers` after provisioning completes."
        )
    lines = [
        f"Which server should I {verb}?",
        "",
        "Please include the exact hostname so AVA does not guess.",
        "",
        "Available AVA-managed servers:",
    ]
    for row in rows:
        lines.append(
            f"- `{row['vm_name']}` - power state `{row.get('power_state', 'not checked')}`, "
            f"provider status `{row.get('provider_status', 'not checked')}`"
        )
    lines.extend(
        [
            "",
            f"Example: `{verb} {rows[0]['vm_name']}`",
        ]
    )
    if operation == "delete_vm":
        lines.append("Delete is high risk and will still require approval before anything is removed.")
    elif operation in {"start_vm", "stop_vm"}:
        lines.append("Power changes still require approval before AVA touches the VM.")
    return "\n".join(lines)


def _format_known_vm_name_conflict(session, row: dict[str, Any]) -> str:
    desired = session.desired_state or {}
    requested_name = desired.get("vm_name") or desired.get("hostname") or "that hostname"
    result = row.get("result")
    vm_name = row.get("vm_name") or requested_name
    if not row.get("ava_managed", True):
        power_state = row.get("power_state") or "unknown"
        provider_status = row.get("provider_status") or "unknown"
        return (
            f"I cannot create `{requested_name}` because a VM with that name already exists in the local infrastructure.\n\n"
            f"- Existing VM: `{vm_name}`\n"
            f"- Requested hostname: `{requested_name}`\n"
            "- Source: `VirtualBox inventory reported by the AVA host runner`\n"
            f"- Power state: `{power_state}`\n"
            f"- Provider status: `{provider_status}`\n\n"
            "Please choose a different hostname, for example `ava-web-04`, or ask `list my servers` if you want to manage an existing server.\n"
            "No approval was created and no runner job was queued."
        )
    ssh = "unknown"
    web_url = "unknown"
    if result and result.ssh_host and result.ssh_port:
        ssh = f"{result.ssh_host}:{result.ssh_port}"
    if result and result.http_port:
        web_url = f"http://127.0.0.1:{result.http_port}/"
    return (
        f"I cannot use hostname `{requested_name}` because AVA already has a completed managed server with that name.\n\n"
        f"- Existing VM: `{vm_name}`\n"
        f"- SSH / PuTTY: `{ssh}`\n"
        f"- Web URL: `{web_url}`\n\n"
        "Choose a different hostname, for example `ava-web-04`, or ask `list my servers` to see current servers.\n"
        "No approval was created and no runner job was queued."
    )


def _format_runner_vm_name_conflict(session, conflict: dict[str, Any], *, approval_recorded: bool = False) -> str:
    desired = session.desired_state or {}
    requested_name = conflict.get("requested_name") or desired.get("vm_name") or desired.get("hostname") or "that hostname"
    vm_name = conflict.get("vm_name") or requested_name
    final_line = (
        "No runner job was queued and no temporary password was issued."
        if approval_recorded
        else "No approval was created and no runner job was queued."
    )
    return (
        f"I cannot create `{requested_name}` because a VM with that name already exists in the local infrastructure.\n\n"
        f"- Existing VM: `{vm_name}`\n"
        f"- Requested hostname: `{requested_name}`\n"
        "- Source: `VirtualBox inventory reported by the AVA host runner`\n\n"
        "Please choose a different hostname, for example `ava-web-04`, or ask `list my servers` "
        "if you want to manage an existing server.\n"
        f"{final_line}"
    )


def _format_runner_unavailable_response() -> str:
    return (
        "I cannot start provisioning yet because the Windows host-side VirtualBox runner is not "
        "reporting healthy.\n\n"
        "No VM was created and no temporary password was issued. Start AVA with "
        "`scripts/start-ava.ps1` or wait for the runner to come online, then approve again."
    )


def _format_approval_recorded_response(session) -> str:
    return (
        "Approval recorded.\n\n"
        f"- Approval ID: `{session.approval_id}`\n"
        "- Status: `approved`\n"
        "- VM created: `no`\n\n"
        "To start provisioning now, reply: `continue provisioning`.\n\n"
        "After that, AVA will queue the Windows host runner and show the live provisioning step. "
        "Provisioning usually takes 3-8 minutes. AVA will report the hostname, SSH/PuTTY port, "
        "and web URL only after bootstrap, hardening, and HTTP verification are complete. "
        "When it is complete, type `open web console` to access the server."
    )


def _effective_phase(session, runner: dict[str, Any] | None) -> str:
    if runner:
        result = runner.get("result")
        progress = runner.get("progress")
        if result and result.error:
            return SessionPhase.FAILED.value
        if _runner_completed(runner):
            return SessionPhase.COMPLETED.value
        if progress and getattr(progress, "status", None):
            return str(getattr(progress, "status"))
    return session.phase.value


def _is_cancel(query: str) -> bool:
    return query in {"cancel", "cancel provisioning", "stop provisioning", "abort provisioning"}


def _extract_role_answer(query: str) -> dict[str, Any]:
    if any(marker in query for marker in ("web", "nginx")):
        return {"role": "web_server"}
    if any(marker in query for marker in ("database", "db", "postgres", "load balancer", "cache", "redis")):
        return {"unsupported_role": True}
    return {}


def _extract_spec_answers(query: str) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    patterns = {
        "cpu": (
            r"\b(?:cpu|cpus|core|cores|vcpu|vcpus)\s*[:=]?\s*(\d{1,3})\b",
            r"\b(\d{1,3})\s*(?:cpu|cpus|core|cores|vcpu|vcpus)\b",
        ),
        "ram_gb": (
            r"\b(?:ram|memory)\s*[:=]?\s*(\d{1,4})\s*(?:gb|g)?\b",
            r"\b(\d{1,4})\s*(?:gb|g)\s*(?:ram|memory)\b",
        ),
        "disk_gb": (
            r"\b(?:disk|storage|volume)\s*[:=]?\s*(\d{1,5})\s*(?:gb|g)?\b",
            r"\b(\d{1,5})\s*(?:gb|g)\s*(?:disk|storage|volume)\b",
        ),
    }
    for field_name, field_patterns in patterns.items():
        for pattern in field_patterns:
            match = re.search(pattern, query)
            if match:
                answers[field_name] = int(match.group(1))
                break

    if "bridged" in query:
        answers["network_mode"] = "bridged"
    elif "host only" in query or "host-only" in query:
        answers["network_mode"] = "hostonly"
    elif "nat" in query:
        answers["network_mode"] = "nat"

    if "internal only" in query or "private firewall" in query:
        answers["firewall_profile"] = "internal_only"
    elif "public" in query or "web public" in query:
        answers["firewall_profile"] = "web_public"

    if "skip hardening" in query or "no hardening" in query:
        answers["hardening_profile"] = "none"
    elif "harden" in query or "baseline" in query:
        answers["hardening_profile"] = "baseline_linux"

    hostname = _extract_hostname(query)
    if hostname:
        answers["vm_name"] = hostname
    return answers


def _is_approval_continuation(query: str) -> bool:
    return any(marker in query for marker in ("continue", "proceed", "approved", "approval", "go ahead", "check approval"))


def _extract_chat_approval_id(query: str) -> str | None:
    match = re.fullmatch(r"(?:approve|approved|confirm approval|approve request)\s+[-:#]?\s*([a-f0-9]{8})", query or "")
    return match.group(1) if match else None


def _is_first_login_confirmation(query: str) -> bool:
    return (
        "logged in" in query
        or "login done" in query
        or "first login" in query
        or ("password" in query and "changed" in query)
    )


def _extract_post_login_answers(query: str) -> dict[str, Any]:
    if "skip hardening" in query or "no hardening" in query or "opt out" in query:
        return {"hardening_profile": "none", "post_login_actions": ["skip_hardening"]}
    if "yes" in query or "harden" in query or "baseline" in query:
        return {"hardening_profile": "baseline_linux", "post_login_actions": ["baseline_linux"]}
    return {}


def _looks_like_hardening_followup(query: str, session) -> bool:
    if any(marker in query for marker in ("harden", "baseline", "skip hardening", "no hardening", "opt out")):
        return True
    return query in {"yes", "yes please", "yes harden it"} and session.phase in {
        SessionPhase.AWAITING_POST_LOGIN_CHOICES,
        SessionPhase.BOOTSTRAPPING,
        SessionPhase.VERIFYING,
        SessionPhase.COMPLETED,
    }


def _extract_hostname(query: str) -> str | None:
    patterns = (
        r"\bhostname\s*[:=]?\s*([a-z0-9][a-z0-9_-]{0,62})\b",
        r"\bhost\s+name\s*[:=]?\s*([a-z0-9][a-z0-9_-]{0,62})\b",
        r"\bvm\s+name\s*[:=]?\s*([a-z0-9][a-z0-9_-]{0,62})\b",
        r"\bname\s+it\s+([a-z0-9][a-z0-9_-]{0,62})\b",
        r"\bcalled\s+([a-z0-9][a-z0-9_-]{0,62})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, query)
        if match:
            return match.group(1).replace("_", "-").lower()
    return None


def _is_status_query(query: str) -> bool:
    return (
        "provisioning status" in query
        or "provisionning status" in query
        or "provision status" in query
        or "vm status" in query
        or "server status" in query
        or "snapshot status" in query
        or "operation status" in query
        or "latest operation" in query
        or "latest snapshot" in query
        or "status of provisioning" in query
        or query in {"status", "show status", "show me status"}
        or (
            "status" in query
            and any(marker in query for marker in ("provision", "provisionning", "vm", "server", "web server", "snapshot", "operation"))
        )
    )


def _is_connection_query(query: str) -> bool:
    return (
        "how do i connect" in query
        or "how to connect" in query
        or "putty" in query
        or "ssh details" in query
        or "ssh connection" in query
        or "connection details" in query
        or "login details" in query
        or "host ip" in query
        or "host/ip" in query
        or query in {"ip", "server ip", "ssh", "connect", "login"}
    )


def _is_evidence_query(query: str) -> bool:
    return (
        "what did you do" in query
        or "evidence" in query
        or "completion report" in query
        or "what have you done" in query
    )


def _is_web_verification_query(query: str) -> bool:
    return (
        "verify the web server" in query
        or "verify web server" in query
        or "check the web server" in query
        or "check web server" in query
    )


def _format_desired_state(desired: dict[str, Any]) -> str:
    if not desired:
        return "Desired state: not ready yet."
    return (
        "Desired state:\n"
        f"- Provider: `{desired.get('provider', 'virtualbox')}`\n"
        f"- OS: `{desired.get('os', 'ubuntu')}`\n"
        f"- Role: `{desired.get('role', 'web_server')}`\n"
        f"- Hostname: `{desired.get('vm_name') or 'auto-generated'}`\n"
        f"- CPU: `{desired.get('cpu')}`\n"
        f"- RAM: `{desired.get('ram_gb')} GB`\n"
        f"- Disk: `{desired.get('disk_gb')} GB`\n"
        f"- Network: `{desired.get('network_mode', 'nat')}`\n"
        f"- Firewall: `{desired.get('firewall_profile', 'web_public')}`\n"
        f"- Hardening: `{desired.get('hardening_profile', 'baseline_linux')}`"
    )


def _connection_lines(
    session,
    result: ProvisioningJobResult | None,
    *,
    heading: str = "Connection details:",
) -> list[str]:
    answers = session.collected_answers or {}
    username = answers.get("username", "avaadmin")
    if not result or not result.instance_id:
        return [
            heading,
            "- SSH host/IP: `pending until runner completes`",
            "- SSH port: `pending until runner completes`",
            f"- Username: `{username}`",
            "- Temporary password: `shown once at approval; not recoverable from status`",
            "- Web URL: `pending until runner completes`",
        ]
    return [
        heading,
        f"- Hostname / VM name: `{result.instance_name or result.instance_id}`",
        f"- SSH host/IP: `{result.ssh_host or 'unknown'}` (PuTTY Host Name)",
        f"- SSH port: `{result.ssh_port or 'unknown'}` (PuTTY Port)",
        f"- Username: `{username}`",
        "- Temporary password: `shown once at approval; not recoverable from status`",
        f"- Web URL: `http://127.0.0.1:{result.http_port}/`" if result.http_port else "- Web URL: `unknown`",
    ]


def _format_hardening_summary(session, result: ProvisioningJobResult | None) -> list[str]:
    answers = session.collected_answers or {}
    desired = session.desired_state or {}
    profile = answers.get("hardening_profile") or desired.get("hardening_profile") or "baseline_linux"
    status = "applied by runner" if result and result.instance_id and profile != "none" else "requested/default"
    if result and result.error:
        status = "not applied; provisioning failed before completion"
    if profile == "none":
        status = "explicitly skipped"
    return [
        "Hardening summary:",
        f"- Server hardening profile: `{profile}` ({status})",
        "- Linux baseline: `default-on unless explicitly skipped`",
        "- Web role hardening: `not verified because provisioning failed`"
        if result and result.error
        else "- Web role hardening: `nginx web_server role verified with HTTP 200`"
        if result and result.instance_id
        else "- Web role hardening: `pending runner completion`",
        "- Apache hardening: `not applied in v2.0.0 because the active role is nginx/web_server`",
    ]


def _latest_live_verify_result(runner: dict[str, Any] | None):
    runner = runner or {}
    operation_result = runner.get("day2_result")
    if operation_result and getattr(operation_result, "operation", None) == "verify":
        return operation_result
    return None


def _format_runtime_truth_lines(runner: dict[str, Any] | None) -> list[str]:
    runner = runner or {}
    result = runner.get("result")
    live_verify = _latest_live_verify_result(runner)
    if live_verify:
        error = getattr(live_verify, "error", None) or {}
        lines = [
            "Runtime truth:",
            "- Stored provisioning result: `available`",
            f"- Latest live verification: `{getattr(live_verify, 'status', 'unknown')}`",
            f"- Live verification checked: `{getattr(live_verify, 'completion_timestamp', 'unknown')}`",
        ]
        if getattr(live_verify, "status", None) != "completed":
            lines.append(
                f"- Live verification error: `{error.get('message') or error.get('failure_class') or 'live verification failed'}`"
            )
        return lines
    if result and result.instance_id:
        return [
            "Runtime truth:",
            "- Stored provisioning result: `available`",
            "- Latest live verification: `not checked in this response`",
            "- Note: stored connection details are last-known history until `verify the web server` passes live.",
        ]
    return []


def _format_connection_response(session, runner: dict[str, Any] | None = None) -> str:
    runner = runner or {}
    result = runner.get("result")
    connection_result = _runner_connection_result(runner)
    lines = [
        "AVA connection details for the active provisioning session:",
        "",
        f"- Runner job ID: `{runner.get('job_id') or 'not queued yet'}`",
        f"- Runner status: `{runner.get('status') or 'not available yet'}`",
        "",
        *_connection_lines(session, connection_result),
    ]
    if result and result.instance_id:
        lines.extend(
            [
                "",
                "PuTTY settings:",
                f"- Host Name: `{result.ssh_host}`",
                f"- Port: `{result.ssh_port}`",
                "- Connection type: `SSH`",
            ]
        )
    elif connection_result and connection_result.instance_id:
        lines.extend(
            [
                "",
                "Note: these are runner progress details. AVA will mark the VM fully complete only "
                "after bootstrap, hardening, and HTTP verification pass.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "The VM connection endpoint is produced after the host runner creates and verifies the VM.",
            ]
        )
    return "\n".join(lines)


def _format_status_response(session, runner: dict[str, Any] | None = None) -> str:
    answers = session.collected_answers or {}
    runner = runner or {}
    result = runner.get("result")
    progress_result = _runner_progress_result(runner)
    failed_error = result.error if result and result.error else None
    failed_rollback = failed_error.get("rollback") if isinstance(failed_error, dict) else None
    failed_cleanup_destroyed = (failed_rollback or {}).get("status") == "destroyed"
    attached_instance = None
    if not failed_cleanup_destroyed:
        attached_instance = session.instance_id or (result.instance_id if result else None) or (progress_result.instance_id if progress_result else None)
    effective_phase = _effective_phase(session, runner)
    lines = [
        "Provisioning status for the active AVA v2 web-server session:",
        "",
        f"- Session ID: `{session.session_id}`",
        f"- Phase: `{effective_phase}`",
        f"- Conversation checkpoint: `{session.phase.value}`",
        f"- Approval ID: `{session.approval_id or 'not queued yet'}`",
        f"- Runner job ID: `{runner.get('job_id') or 'not queued yet'}`",
        f"- Runner status: `{runner.get('status') or 'not available yet'}`",
        f"- Hostname: `{(session.desired_state or {}).get('vm_name') or 'auto-generated'}`",
        f"- Temporary credential issued: `{'yes' if session.credential_id else 'no'}`",
        "- Temporary password: `shown once at approval; not recoverable from status`",
        f"- First-login confirmation: `{'yes' if session.phase in {SessionPhase.AWAITING_POST_LOGIN_CHOICES, SessionPhase.BOOTSTRAPPING, SessionPhase.VERIFYING, SessionPhase.COMPLETED} else 'pending'}`",
        f"- Hardening choice: `{answers.get('hardening_profile', 'not recorded yet')}`",
        f"- Attached VM instance: `{attached_instance or 'none yet'}`",
        f"- Timestamp: `{_utc_now()}`",
        "",
        _format_desired_state(session.desired_state or {}),
    ]
    if result and result.error:
        error = result.error or {}
        rollback = error.get("rollback") if isinstance(error, dict) else None
        display_failure_class = error.get("failure_class") or "runner_failed"
        display_error = error.get("message") or "unknown error"
        if _is_vm_name_conflict_error(error):
            display_failure_class = "vm_name_conflict"
            display_error = (
                "A VM with the requested hostname already exists in VirtualBox. "
                "Choose a different hostname, or manage/delete the existing VM before retrying."
            )
        lines.extend(
            [
                "",
                "Failure details:",
                f"- Failed step: `{error.get('failed_step') or 'host_runner'}`",
                f"- Failure class: `{display_failure_class}`",
                f"- Error: `{display_error}`",
                f"- Partial VM cleanup: `{(rollback or {}).get('status') or 'not reported'}`",
                "",
                "No active VM is attached to this session, and no PuTTY or web URL is available because "
                "this VM did not finish provisioning. "
                "You can start a fresh request now.",
            ]
        )
    elif result and result.instance_id:
        lines.extend(
            [
                "",
                *_format_runtime_truth_lines(runner),
                "",
                *_connection_lines(session, result, heading="Last-known connection details:"),
                "",
                *_format_hardening_summary(session, result),
            ]
        )
        operation_lines = _format_latest_server_operation_lines(runner)
        if operation_lines:
            lines.extend(["", *operation_lines])
    elif progress_result and progress_result.instance_id:
        lines.extend(
            [
                "",
                *_runner_progress_lines(runner),
                "",
                *_runner_progress_guidance_lines(runner),
                "",
                *_connection_lines(session, progress_result, heading="Current runner progress connection details:"),
                "",
                "Completion boundary: the VM is visible to AVA, but final provisioning is still waiting "
                "for bootstrap, hardening, and HTTP verification evidence. Web Console access is intentionally "
                "held back until this session reaches `completed`.",
            ]
        )
    elif runner.get("job_id"):
        lines.extend(
            [
                "",
                *_runner_progress_guidance_lines(runner),
                "",
                *_connection_lines(session, None),
                "",
                "Runner boundary: the approved job is queued or running. AVA will show PuTTY "
                "connection details after the host runner writes the result. Web Console access becomes "
                "available after the completed verification result is stored.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Execution boundary: this live chat session has collected and approved the plan, "
                "but no VM instance is attached yet. The host-side VirtualBox runner must execute "
                "the approved plan before AVA can report a created VM.",
            ]
        )
    return "\n".join(lines)


def _format_verification_response(session, runner: dict[str, Any] | None = None) -> str:
    runner = runner or {}
    result = runner.get("result")
    progress_result = _runner_progress_result(runner)
    if result and result.error:
        return (
            "Web-server verification failed for the host runner job.\n\n"
            f"- Job ID: `{runner.get('job_id')}`\n"
            f"- Status: `{runner.get('status') or 'failed'}`\n"
            f"- Error: `{result.error.get('message') or result.error.get('failure_class') or 'runner_failed'}`\n"
            f"- Timestamp: `{result.completion_timestamp}`"
        )
    if result and result.instance_id:
        live_verify = _latest_live_verify_result(runner)
        if live_verify:
            return format_live_verify_response(live_verify, result=result)
        return (
            "Live verification is required for current server truth.\n\n"
            "AVA has stored provisioning evidence for this VM, but stored history is not proof "
            "that the VM still exists, is running, or is serving HTTP now.\n\n"
            "Last-known provisioning evidence:\n\n"
            f"- Instance: `{result.instance_id}`\n"
            f"- Hostname / VM name: `{result.instance_name or result.instance_id}`\n"
            f"- SSH / PuTTY: `{result.ssh_host}:{result.ssh_port}`\n"
            f"- Username: `{(session.collected_answers or {}).get('username', 'avaadmin')}`\n"
            f"- HTTP: `http://127.0.0.1:{result.http_port}/`\n"
            f"- Completed: `{result.completion_timestamp}`\n\n"
            "No current pass/fail checks are shown here because this response did not receive "
            "fresh live verification evidence.\n\n"
            "Ask `verify the web server` with the Windows host runner online to produce fresh live evidence."
        )
    if progress_result and progress_result.instance_id:
        http_line = (
            f"- HTTP: `http://127.0.0.1:{progress_result.http_port}/`"
            if progress_result.http_port
            else "- HTTP: `pending`"
        )
        return "\n".join(
            [
                "The VM is visible to the host runner, but web-server verification is not complete yet.",
                "",
                f"- Job ID: `{runner.get('job_id')}`",
                f"- Runner status: `{runner.get('status') or 'working'}`",
                f"- Stage: `{getattr(runner.get('progress'), 'stage', 'unknown')}`",
                f"- VM: `{progress_result.instance_name or progress_result.instance_id}`",
                f"- SSH / PuTTY: `{progress_result.ssh_host or 'pending'}:{progress_result.ssh_port or 'pending'}`",
                http_line,
                "",
                "AVA will show final verification after nginx and host HTTP checks pass.",
            ]
        )
    if runner.get("job_id"):
        return (
            "The web-server verification is not complete yet.\n\n"
            f"- Job ID: `{runner.get('job_id')}`\n"
            f"- Runner status: `{runner.get('status') or 'queued'}`\n"
            "- PuTTY details will be available after the host runner completes VM creation and verification."
        )
    if not session.instance_id:
        return (
            "I cannot verify nginx/web health for this chat-created session yet because no VM "
            "instance is attached to it.\n\n"
            "Current evidence:\n"
            f"- Session phase: `{session.phase.value}`\n"
            f"- Approval ID: `{session.approval_id or 'not queued yet'}`\n"
            f"- Desired role: `{(session.desired_state or {}).get('role', session.role or 'web_server')}`\n"
            f"- Attached VM instance: `none yet`\n"
            f"- Timestamp: `{_utc_now()}`\n\n"
            "Next required step: run the host-side VirtualBox provisioning runner for this approved "
            "plan. After a VM is created and attached to the session, AVA can provide the PuTTY "
            "connection host/IP, SSH port, username, and then verify SSH, nginx, guest HTTP, and "
            "host HTTP evidence."
        )
    return (
        "Verification is ready to run for the attached VM, but this chat route currently reports "
        "stored session evidence only. Full live verification is performed by the host-side "
        "VirtualBox runner and verification engine."
    )


def _format_evidence_response(session, runner: dict[str, Any] | None = None) -> str:
    answers = session.collected_answers or {}
    runner = runner or {}
    result = runner.get("result")
    progress_result = _runner_progress_result(runner)
    effective_phase = _effective_phase(session, runner)
    failed_error = result.error if result and result.error else None
    failed_rollback = failed_error.get("rollback") if isinstance(failed_error, dict) else None
    failed_cleanup_destroyed = (failed_rollback or {}).get("status") == "destroyed"
    attached_instance = None
    if not failed_cleanup_destroyed:
        attached_instance = session.instance_id or (result.instance_id if result else None) or (progress_result.instance_id if progress_result else None)
    evidence = [
        "Provisioning evidence for the active AVA v2 session:",
        "",
        f"- Intent captured: `create_vm`",
        f"- Role selected: `{session.role or (session.desired_state or {}).get('role', 'web_server')}`",
        f"- Hostname: `{(session.desired_state or {}).get('vm_name') or 'auto-generated'}`",
        f"- Desired state ready: `{'yes' if session.desired_state else 'no'}`",
        f"- Approval queued: `{'yes' if session.approval_id else 'no'}`",
        f"- Runner job ID: `{runner.get('job_id') or 'not queued yet'}`",
        f"- Runner status: `{runner.get('status') or 'not available yet'}`",
        f"- Temporary credential issued once: `{'yes' if session.credential_id else 'no'}`",
        f"- Effective phase: `{effective_phase}`",
        f"- Conversation checkpoint: `{session.phase.value}`",
        f"- Recorded hardening profile: `{answers.get('hardening_profile', 'not recorded yet')}`",
        f"- Attached VM instance: `{attached_instance or 'none yet'}`",
        f"- Evidence timestamp: `{_utc_now()}`",
        "",
        _format_desired_state(session.desired_state or {}),
    ]
    if result and result.error:
        error = result.error or {}
        rollback = error.get("rollback") if isinstance(error, dict) else None
        failed_instance = result.instance_id or result.instance_name or session.instance_id
        evidence.extend(
            [
                "",
                "Runner failure evidence:",
                f"- Failed step: `{error.get('failed_step') or 'host_runner'}`",
                f"- Failure class: `{error.get('failure_class') or 'runner_failed'}`",
                f"- Error: `{error.get('message') or 'unknown error'}`",
                f"- Partial VM cleanup: `{(rollback or {}).get('status') or 'not reported'}`",
                f"- Historical failed VM identity: `{failed_instance or 'not available'}`",
                f"- Completion timestamp: `{result.completion_timestamp}`",
            ]
        )
    elif result and result.instance_id:
        evidence.extend(
            [
                "",
                *_format_runtime_truth_lines(runner),
                "",
                "Stored runner result evidence:",
                f"- Instance name: `{result.instance_name or result.instance_id}`",
                f"- SSH host/IP: `{result.ssh_host or 'unknown'}` (PuTTY Host Name)",
                f"- SSH port: `{result.ssh_port or 'unknown'}` (PuTTY Port)",
                f"- Username: `{answers.get('username', 'avaadmin')}`",
                "- Temporary password: `shown once at approval; not recoverable from evidence`",
                f"- HTTP port: `{result.http_port or 'unknown'}`",
                f"- Web URL: `http://127.0.0.1:{result.http_port}/`" if result.http_port else "- Web URL: `unknown`",
                f"- Completion timestamp: `{result.completion_timestamp}`",
                "",
                *_format_hardening_summary(session, result),
            ]
        )
        operation_lines = _format_latest_server_operation_lines(runner)
        if operation_lines:
            evidence.extend(["", *operation_lines])
    elif progress_result and progress_result.instance_id:
        evidence.extend(
            [
                "",
                "Runner progress evidence:",
                f"- Stage: `{getattr(runner.get('progress'), 'stage', 'unknown')}`",
                f"- VM: `{progress_result.instance_name or progress_result.instance_id}`",
                f"- SSH host/IP: `{progress_result.ssh_host or 'pending'}`",
                f"- SSH port: `{progress_result.ssh_port or 'pending'}`",
                f"- HTTP port: `{progress_result.http_port or 'pending'}`",
                f"- Updated: `{getattr(runner.get('progress'), 'updated_at', 'unknown')}`",
                "",
                "Important: this is in-progress VM evidence, not final web-server completion evidence. "
                "Final evidence appears only after bootstrap, hardening, and HTTP verification pass.",
            ]
        )
    elif not session.instance_id:
        evidence.extend(
            [
                "",
                "Important: this is conversation and approval evidence, not VM creation evidence. "
                "No VM creation evidence exists for this chat session until the host-side "
                "VirtualBox runner executes the approved plan.",
            ]
        )
    return "\n".join(evidence)


def _format_latest_server_operation_lines(runner: dict[str, Any] | None) -> list[str]:
    runner = runner or {}
    operation_id = runner.get("day2_operation_id")
    if not operation_id:
        return []
    operation_result = runner.get("day2_result")
    operation_status = runner.get("day2_status") or "queued"
    if operation_result:
        evidence = dict(getattr(operation_result, "evidence", {}) or {})
        lines = [
            "Latest server-management operation:",
            f"- Operation ID: `{operation_id}`",
            f"- Operation: `{getattr(operation_result, 'operation', 'unknown')}`",
            f"- Status: `{getattr(operation_result, 'status', operation_status)}`",
            f"- Completed: `{getattr(operation_result, 'completion_timestamp', 'unknown')}`",
        ]
        if evidence.get("snapshot_name"):
            lines.append(f"- Snapshot: `{evidence.get('snapshot_name')}`")
        if getattr(operation_result, "error", None):
            error = getattr(operation_result, "error") or {}
            lines.append(f"- Error: `{error.get('message') or error.get('failure_class') or 'operation_failed'}`")
        return lines
    return [
        "Latest server-management operation:",
        f"- Operation ID: `{operation_id}`",
        f"- Status: `{operation_status}`",
    ]


def _format_flow_response(response) -> str:
    session = response.session
    if response.credential:
        credential = response.credential
        return (
            "Approval confirmed. Temporary access has been issued once for this approved provisioning plan.\n\n"
            f"Username: `{credential.username}`\n"
            f"Temporary password: `{credential.temporary_password}`\n\n"
            "Save this password now. AVA will not print it again in status or evidence responses.\n\n"
            "Provisioning has been handed to the host-side VirtualBox runner. AVA will report the "
            "hostname, PuTTY SSH host/IP, SSH port, and web URL as soon as the runner creates, "
            "bootstraps, hardens, and verifies the VM.\n\n"
            "After AVA reports the PuTTY details, log in with the temporary password and change it "
            "on first login. Then reply: "
            "`I logged in and changed the password`."
        )
    if response.requires_approval and response.approval_id:
        desired = session.desired_state or {}
        return (
            "Plan ready for approval.\n\n"
            f"Provider: `{desired.get('provider', 'virtualbox')}`\n"
            f"OS: `{desired.get('os', 'ubuntu')}`\n"
            f"Role: `{desired.get('role', 'web_server')}`\n"
            f"Hostname: `{desired.get('vm_name') or 'auto-generated'}`\n"
            f"CPU: `{desired.get('cpu')}`\n"
            f"RAM: `{desired.get('ram_gb')} GB`\n"
            f"Disk: `{desired.get('disk_gb')} GB`\n"
            f"Network: `{desired.get('network_mode', 'nat')}`\n"
            f"Hardening: `{desired.get('hardening_profile', 'baseline_linux')}`\n\n"
            f"Approval ID: `{response.approval_id}`\n"
            f"To approve this plan, reply: `approve {response.approval_id}`.\n"
            "No VM is created until this approval is accepted.\n"
            "After approval is recorded, reply: `continue provisioning` to start VM provisioning."
        )
    if response.missing_fields:
        readable = ", ".join(f"`{field}`" for field in response.missing_fields)
        return (
            f"{response.message}\n\n"
            f"Please provide: {readable}.\n"
            "Example: `2 CPU, 4 GB RAM, 30 GB disk, hostname ava-web-01`.\n"
            "Optional: include `hostname <name>` if you want a specific VM hostname."
        )
    if session.phase == SessionPhase.AWAITING_VM_TYPE:
        return response.message + "\n\nAvailable in v2.0.0: `web_server` only."
    return response.message
