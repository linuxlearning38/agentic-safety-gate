"""Serving-layer adapter for AVA v2 guided provisioning conversations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from control import approval
from provisioning.conversation import ProvisioningFlowEngine, SessionManager, SessionPhase
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

        if route_intent == "provisioning":
            response = self.flow.start(user_id, query)
            if response.requires_approval and response.desired_state_ready:
                response = self.flow.request_approval(response.session.session_id)
            return self._result(_format_flow_response(response), response.session, approval_id=response.approval_id)

        if not active:
            return ProvisioningServingResult(handled=False)

        runner = self._runner_snapshot(active)

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
                if not queue_entry or queue_entry.get("status") != "pending":
                    status = queue_entry.get("status") if queue_entry else "not_found"
                    return self._result(
                        f"Approval `{approval_id}` is not pending; current status is `{status}`.",
                        active,
                    )
                if not _runner_is_healthy(self.job_queue):
                    return self._result(_format_runner_unavailable_response(), active)
                approval.update_status(approval_id, "approved")
                response = self.flow.continue_after_approval(active.session_id)
                message = self._format_approval_and_enqueue(response)
                return self._result(message, response.session, approval_id=response.approval_id)
            if not _is_approval_continuation(normalized):
                return ProvisioningServingResult(handled=False)
            if not _runner_is_healthy(self.job_queue):
                return self._result(_format_runner_unavailable_response(), active)
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

    def _maybe_queue_approval(self, response):
        if response.requires_approval and response.desired_state_ready:
            response = self.flow.request_approval(response.session.session_id)
        return self._result(_format_flow_response(response), response.session, approval_id=response.approval_id)

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
                + "\n\nRunner job queued for host-side VirtualBox execution.\n"
                + f"Job ID: `{job.job_id}`\n"
                + "AVA will report the PuTTY SSH host/IP and port after the host runner creates the VM."
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
        if not job_id:
            return {"job_id": None, "status": None, "result": None}
        status = self.job_queue.get_status(job_id)
        result = self.job_queue.get_result(job_id)
        if result and result.instance_id:
            status = "completed"
        elif result and result.error:
            status = "failed"
        if result and result.instance_id and not session.instance_id:
            self.sessions.save(session.with_updates(instance_id=result.instance_id))
        return {"job_id": job_id, "status": status, "result": result}

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


def _runner_is_healthy(job_queue: ProvisioningJobQueue) -> bool:
    health_check = getattr(job_queue, "is_runner_healthy", None)
    if health_check is None:
        return True
    try:
        return bool(health_check())
    except Exception:
        return False


def _format_runner_unavailable_response() -> str:
    return (
        "I cannot start provisioning yet because the Windows host-side VirtualBox runner is not "
        "reporting healthy.\n\n"
        "No VM was created and no temporary password was issued. Start AVA with "
        "`scripts/start-ava.ps1` or wait for the runner to come online, then approve again."
    )


def _effective_phase(session, runner: dict[str, Any] | None) -> str:
    if runner:
        result = runner.get("result")
        if result and result.error:
            return SessionPhase.FAILED.value
        if _runner_completed(runner):
            return SessionPhase.COMPLETED.value
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
        or "status of provisioning" in query
        or query in {"status", "show status", "show me status"}
        or ("status" in query and any(marker in query for marker in ("provision", "provisionning", "vm", "server", "web server")))
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


def _connection_lines(session, result: ProvisioningJobResult | None) -> list[str]:
    answers = session.collected_answers or {}
    username = answers.get("username", "avaadmin")
    if not result or not result.instance_id:
        return [
            "Connection details:",
            "- SSH host/IP: `pending until runner completes`",
            "- SSH port: `pending until runner completes`",
            f"- Username: `{username}`",
            "- Temporary password: `shown once at approval; not recoverable from status`",
            "- Web URL: `pending until runner completes`",
        ]
    return [
        "Connection details:",
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
    if profile == "none":
        status = "explicitly skipped"
    return [
        "Hardening summary:",
        f"- Server hardening profile: `{profile}` ({status})",
        "- Linux baseline: `default-on unless explicitly skipped`",
        "- Web role hardening: `nginx web_server role verified with HTTP 200`" if result and result.instance_id else "- Web role hardening: `pending runner completion`",
        "- Apache hardening: `not applied in v2.0.0 because the active role is nginx/web_server`",
    ]


def _format_connection_response(session, runner: dict[str, Any] | None = None) -> str:
    runner = runner or {}
    result = runner.get("result")
    lines = [
        "AVA connection details for the active provisioning session:",
        "",
        f"- Runner job ID: `{runner.get('job_id') or 'not queued yet'}`",
        f"- Runner status: `{runner.get('status') or 'not available yet'}`",
        "",
        *_connection_lines(session, result),
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
        f"- Attached VM instance: `{session.instance_id or (result.instance_id if result else None) or 'none yet'}`",
        f"- Timestamp: `{_utc_now()}`",
        "",
        _format_desired_state(session.desired_state or {}),
    ]
    if result and result.instance_id:
        lines.extend(["", *_connection_lines(session, result), "", *_format_hardening_summary(session, result)])
    elif runner.get("job_id"):
        lines.extend(
            [
                "",
                *_connection_lines(session, None),
                "",
                "Runner boundary: the approved job is queued or running. AVA will show PuTTY "
                "connection details after the host runner writes the result.",
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
    if result and result.error:
        return (
            "Web-server verification failed for the host runner job.\n\n"
            f"- Job ID: `{runner.get('job_id')}`\n"
            f"- Status: `{runner.get('status') or 'failed'}`\n"
            f"- Error: `{result.error.get('message') or result.error.get('failure_class') or 'runner_failed'}`\n"
            f"- Timestamp: `{result.completion_timestamp}`"
        )
    if result and result.instance_id:
        checks = (result.verification_evidence or {}).get("checks") or []
        check_lines = [
            f"- {check.get('name')}: `{'passed' if check.get('passed') else 'failed'}` ({check.get('evidence', '')})"
            for check in checks[:8]
            if isinstance(check, dict)
        ]
        if not check_lines:
            check_lines = ["- verification evidence recorded by host runner"]
        return (
            "Web-server verification evidence from the host runner:\n\n"
            f"- Instance: `{result.instance_id}`\n"
            f"- Hostname / VM name: `{result.instance_name or result.instance_id}`\n"
            f"- SSH / PuTTY: `{result.ssh_host}:{result.ssh_port}`\n"
            f"- Username: `{(session.collected_answers or {}).get('username', 'avaadmin')}`\n"
            f"- HTTP: `http://127.0.0.1:{result.http_port}/`\n"
            f"- Completed: `{result.completion_timestamp}`\n\n"
            + "\n".join(check_lines)
            + "\n\n"
            + "\n".join(_format_hardening_summary(session, result))
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
    effective_phase = _effective_phase(session, runner)
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
        f"- Attached VM instance: `{session.instance_id or (result.instance_id if result else None) or 'none yet'}`",
        f"- Evidence timestamp: `{_utc_now()}`",
        "",
        _format_desired_state(session.desired_state or {}),
    ]
    if result and result.instance_id:
        evidence.extend(
            [
                "",
                "Runner result evidence:",
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
            "No VM is created until this approval is accepted. After approval, reply: `continue provisioning`."
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
