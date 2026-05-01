"""Deterministic guided-flow engine for AVA v2 provisioning."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from provisioning.credentials import CredentialManager, TemporaryCredential
from provisioning.desired_state import DesiredState, DesiredStateError, missing_required_specs
from provisioning.policy import has_recent_provisioning_approval, queue_provisioning_approval

from .session_manager import ProvisioningSession, SessionManager, SessionPhase


@dataclass(slots=True)
class FlowResponse:
    """Result returned after each guided-flow transition."""

    session: ProvisioningSession
    message: str
    missing_fields: list[str] = field(default_factory=list)
    desired_state_ready: bool = False
    requires_approval: bool = False
    approval_id: str | None = None
    credential: TemporaryCredential | None = None
    error: str | None = None


class ProvisioningFlowEngine:
    """Tiny FSM for v2.0.0 guided provisioning conversations."""

    def __init__(self, sessions: SessionManager, credentials: CredentialManager | None = None):
        self.sessions = sessions
        self.credentials = credentials or CredentialManager(Path(sessions.db_path).with_name("credentials.sqlite3"))

    def start(self, user_id: str, request_text: str) -> FlowResponse:
        session = self.sessions.create_session(user_id=user_id)
        inferred = self._infer_initial_answers(request_text)
        if inferred:
            session = self.sessions.record_answers(session.session_id, inferred)
        return self._advance(session.session_id)

    def submit_answers(self, session_id: str, answers: Dict[str, Any]) -> FlowResponse:
        session = self.sessions.require(session_id)
        if session.phase in {SessionPhase.COMPLETED, SessionPhase.FAILED, SessionPhase.CANCELLED}:
            return FlowResponse(
                session=session,
                message=f"This provisioning session is already {session.phase.value}.",
                error="terminal_session",
            )
        self.sessions.record_answers(session_id, answers)
        return self._advance(session_id)

    def cancel(self, session_id: str) -> FlowResponse:
        session = self.sessions.cancel(session_id)
        return FlowResponse(session=session, message="Provisioning request cancelled.")

    def resume(self, session_id: str) -> FlowResponse:
        return self._advance(session_id)

    def request_approval(self, session_id: str) -> FlowResponse:
        session = self.sessions.require(session_id)
        if session.phase != SessionPhase.AWAITING_APPROVAL or not session.desired_state:
            return FlowResponse(
                session=session,
                message="This provisioning session is not ready for approval yet.",
                error="approval_not_ready",
            )
        if session.approval_id:
            return FlowResponse(
                session=session,
                message="Approval is already queued for this provisioning request.",
                requires_approval=True,
                approval_id=session.approval_id,
            )

        approval_id = queue_provisioning_approval(
            session_id=session.session_id,
            user_id=session.user_id,
            desired_state=session.desired_state,
            query="Create VirtualBox Ubuntu web server",
        )
        session = self.sessions.save(session.with_updates(approval_id=approval_id))
        return FlowResponse(
            session=session,
            message="Approval required before AVA provisions this VirtualBox Ubuntu web server.",
            requires_approval=True,
            approval_id=approval_id,
        )

    def continue_after_approval(self, session_id: str) -> FlowResponse:
        session = self.sessions.require(session_id)
        if session.phase != SessionPhase.AWAITING_APPROVAL:
            return FlowResponse(
                session=session,
                message=f"This session is in {session.phase.value}, not awaiting approval.",
                error="approval_not_expected",
            )

        approved, approved_id = has_recent_provisioning_approval(session.session_id)
        if not approved:
            return FlowResponse(
                session=session,
                message="Provisioning is still waiting for operator approval.",
                requires_approval=True,
                approval_id=session.approval_id,
                error="approval_pending",
            )

        credential = self.credentials.issue_temporary_credential(session.session_id)
        answers = dict(session.collected_answers)
        answers["username"] = credential.username
        session = self.sessions.save(
            session.with_updates(
                phase=SessionPhase.AWAITING_FIRST_LOGIN,
                approval_id=session.approval_id or approved_id,
                credential_id=credential.credential_id,
                collected_answers=answers,
            )
        )
        return FlowResponse(
            session=session,
            message="Provisioning may continue. Temporary access has been issued once; user must change it after first login.",
            credential=credential,
        )

    def confirm_first_login(self, session_id: str) -> FlowResponse:
        session = self.sessions.require(session_id)
        if session.phase != SessionPhase.AWAITING_FIRST_LOGIN:
            return FlowResponse(
                session=session,
                message=f"This session is in {session.phase.value}, not awaiting first-login confirmation.",
                error="first_login_not_expected",
            )
        session = self.sessions.save(session.with_updates(phase=SessionPhase.AWAITING_POST_LOGIN_CHOICES))
        return FlowResponse(
            session=session,
            message="First login confirmed. AVA can now continue with post-login choices and hardening.",
        )

    def _advance(self, session_id: str) -> FlowResponse:
        session = self.sessions.require(session_id)
        answers = dict(session.collected_answers)

        if not answers.get("role"):
            session = self.sessions.save(
                session.with_updates(
                    phase=SessionPhase.AWAITING_VM_TYPE,
                    provider=answers.get("provider"),
                    role=None,
                )
            )
            return FlowResponse(
                session=session,
                message="What type of VM do you want? For v2.0.0 I can build a web server.",
                missing_fields=["role"],
            )

        missing = missing_required_specs(answers)
        if missing:
            session = self.sessions.save(
                session.with_updates(
                    phase=SessionPhase.AWAITING_SPECS,
                    provider=answers.get("provider") or "virtualbox",
                    role=answers.get("role"),
                )
            )
            return FlowResponse(
                session=session,
                message=f"I need these specs before I can plan the VM: {', '.join(missing)}.",
                missing_fields=missing,
            )

        try:
            desired_state = DesiredState.from_answers(answers)
        except DesiredStateError as exc:
            session = self.sessions.save(session.with_updates(phase=SessionPhase.FAILED))
            return FlowResponse(
                session=session,
                message=str(exc),
                error="invalid_desired_state",
            )

        session = self.sessions.save(
            session.with_updates(
                phase=SessionPhase.AWAITING_APPROVAL,
                provider=desired_state.provider,
                role=desired_state.role,
                desired_state=desired_state.to_dict(),
            )
        )
        return FlowResponse(
            session=session,
            message="I have enough information to request approval for this VirtualBox Ubuntu web server.",
            desired_state_ready=True,
            requires_approval=True,
        )

    def _infer_initial_answers(self, request_text: str) -> Dict[str, Any]:
        text = (request_text or "").lower()
        answers: Dict[str, Any] = {
            "provider": "virtualbox",
            "os": "ubuntu",
            "network_mode": "nat",
            "firewall_profile": "web_public",
            "hardening_profile": "baseline_linux",
        }
        if "web" in text or "nginx" in text:
            answers["role"] = "web_server"
        if "ubuntu" in text:
            answers["os"] = "ubuntu"
        return answers
