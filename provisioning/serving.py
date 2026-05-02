"""Serving-layer adapter for AVA v2 guided provisioning conversations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from control import approval
from provisioning.conversation import ProvisioningFlowEngine, SessionManager, SessionPhase


@dataclass(slots=True)
class ProvisioningServingResult:
    """User-facing response returned to AVA's `/ask` serving contract."""

    handled: bool
    response: str = ""
    confidence: str = "high"
    metadata: dict[str, Any] = field(default_factory=dict)


class ProvisioningChatService:
    """Bridge natural chat turns into the v2 provisioning FSM."""

    def __init__(self, db_path: str | Path):
        self.sessions = SessionManager(db_path)
        self.flow = ProvisioningFlowEngine(self.sessions)

    def handle(self, user_id: str, query: str, *, route_intent: str | None = None) -> ProvisioningServingResult:
        user_id = str(user_id or "default")
        query = (query or "").strip()
        normalized = _normalize(query)
        active = self._active_session(user_id)

        if active and _is_cancel(normalized):
            response = self.flow.cancel(active.session_id)
            return self._result(response.message, response.session)

        if route_intent == "provisioning":
            response = self.flow.start(user_id, query)
            if response.requires_approval and response.desired_state_ready:
                response = self.flow.request_approval(response.session.session_id)
            return self._result(_format_flow_response(response), response.session, approval_id=response.approval_id)

        if not active:
            return ProvisioningServingResult(handled=False)

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
                approval.update_status(approval_id, "approved")
                response = self.flow.continue_after_approval(active.session_id)
                return self._result(_format_flow_response(response), response.session, approval_id=response.approval_id)
            if not _is_approval_continuation(normalized):
                return ProvisioningServingResult(handled=False)
            response = self.flow.continue_after_approval(active.session_id)
            return self._result(_format_flow_response(response), response.session, approval_id=response.approval_id)

        if phase == SessionPhase.AWAITING_FIRST_LOGIN:
            if not _is_first_login_confirmation(normalized):
                return ProvisioningServingResult(handled=False)
            response = self.flow.confirm_first_login(active.session_id)
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
    return answers


def _is_approval_continuation(query: str) -> bool:
    return any(marker in query for marker in ("continue", "proceed", "approved", "approval", "go ahead", "check approval"))


def _extract_chat_approval_id(query: str) -> str | None:
    match = re.fullmatch(r"(?:approve|approved|confirm approval|approve request)\s+([a-f0-9]{8})", query or "")
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


def _format_flow_response(response) -> str:
    session = response.session
    if response.credential:
        credential = response.credential
        return (
            "Approval confirmed. Temporary access has been issued once.\n\n"
            f"Username: `{credential.username}`\n"
            f"Temporary password: `{credential.temporary_password}`\n\n"
            "Change this password on first login. After you log in and change it, reply: "
            "`I logged in and changed the password`."
        )
    if response.requires_approval and response.approval_id:
        desired = session.desired_state or {}
        return (
            "Plan ready for approval.\n\n"
            f"Provider: `{desired.get('provider', 'virtualbox')}`\n"
            f"OS: `{desired.get('os', 'ubuntu')}`\n"
            f"Role: `{desired.get('role', 'web_server')}`\n"
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
            "Example: `2 CPU, 4 GB RAM, 30 GB disk`."
        )
    if session.phase == SessionPhase.AWAITING_VM_TYPE:
        return response.message + "\n\nAvailable in v2.0.0: `web_server` only."
    return response.message
