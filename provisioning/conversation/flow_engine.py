"""Deterministic guided-flow engine for AVA v2 provisioning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from provisioning.desired_state import DesiredState, DesiredStateError, missing_required_specs

from .session_manager import ProvisioningSession, SessionManager, SessionPhase


@dataclass(slots=True)
class FlowResponse:
    """Result returned after each guided-flow transition."""

    session: ProvisioningSession
    message: str
    missing_fields: list[str] = field(default_factory=list)
    desired_state_ready: bool = False
    requires_approval: bool = False
    error: str | None = None


class ProvisioningFlowEngine:
    """Tiny FSM for v2.0.0 guided provisioning conversations."""

    def __init__(self, sessions: SessionManager):
        self.sessions = sessions

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
