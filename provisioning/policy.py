"""Policy and approval helpers for AVA v2 provisioning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from control import approval
from provisioning.desired_state import DesiredState, DesiredStateError


PROVISIONING_APPROVAL_ACTION = "provision_virtualbox_ubuntu_web_server"


@dataclass(slots=True)
class ProvisioningPolicyDecision:
    """Deterministic policy result for the first v2 provisioning slice."""

    effect: str
    reason: str
    risk: str = "medium"
    approval_required: bool = True
    violations: list[str] = field(default_factory=list)


def evaluate_provisioning_policy(desired_state: Dict[str, Any] | DesiredState) -> ProvisioningPolicyDecision:
    """Evaluate whether a desired state is allowed to reach approval."""

    try:
        if isinstance(desired_state, DesiredState):
            desired = desired_state
            desired.validate()
        else:
            desired = DesiredState.from_answers(desired_state)
    except DesiredStateError as exc:
        return ProvisioningPolicyDecision(
            effect="block",
            reason=str(exc),
            risk="high",
            approval_required=False,
            violations=[str(exc)],
        )

    if desired.provider != "virtualbox" or desired.os != "ubuntu" or desired.role != "web_server":
        violation = "v2.0.0 policy only allows VirtualBox + Ubuntu + web_server"
        return ProvisioningPolicyDecision(
            effect="block",
            reason=violation,
            risk="high",
            approval_required=False,
            violations=[violation],
        )

    return ProvisioningPolicyDecision(
        effect="require_approval",
        reason="v2.0.0 provisioning changes local infrastructure and requires operator approval.",
        risk="medium",
        approval_required=True,
    )


def approval_key_for_session(session_id: str) -> str:
    """Return the stable approval key for one provisioning session."""

    return f"provisioning:{PROVISIONING_APPROVAL_ACTION}:{session_id}"


def queue_provisioning_approval(
    *,
    session_id: str,
    user_id: str,
    desired_state: Dict[str, Any],
    query: str = "Provision VirtualBox Ubuntu web server",
) -> str:
    """Queue an approval request using AVA's existing approval queue."""

    decision = evaluate_provisioning_policy(desired_state)
    if decision.effect == "block":
        raise PermissionError(decision.reason)

    return approval.add_request(
        PROVISIONING_APPROVAL_ACTION,
        query,
        risk=decision.risk,
        mode="provisioning",
        approval_key=approval_key_for_session(session_id),
        metadata={
            "session_id": session_id,
            "user_id": user_id,
            "desired_state": desired_state,
            "policy_effect": decision.effect,
            "policy_reason": decision.reason,
        },
    )


def has_recent_provisioning_approval(session_id: str, minutes: int = 30) -> tuple[bool, str | None]:
    """Check whether the provisioning session was approved recently."""

    return approval.check_recent_approval(approval_key_for_session(session_id), minutes=minutes)
