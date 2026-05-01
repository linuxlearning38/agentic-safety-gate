"""Failure reporting and rollback helpers for AVA v2 provisioning."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from provisioning.adapters.base import ProviderAdapter


ROLLBACK_ACTIONS = {"destroy_partial_vm", "retain_for_debug", "none"}
ROLLBACK_STATUSES = {"destroyed", "retained", "not_needed", "failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class RollbackReport:
    """Evidence for cleanup after a provisioning failure."""

    action: str
    status: str
    evidence: str
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProvisioningFailureReport:
    """User-safe failure report for a failed provisioning lifecycle step."""

    session_id: str
    phase: str
    failed_step: str
    failure_class: str
    message: str
    instance_id: str | None
    rollback: RollbackReport
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["rollback"] = self.rollback.to_dict()
        return result


class ProvisioningRollbackManager:
    """Apply v2 rollback policy through the provider adapter contract."""

    def __init__(self, adapter: ProviderAdapter):
        self.adapter = adapter

    def handle_failure(
        self,
        *,
        session_id: str,
        phase: str,
        failed_step: str,
        failure_class: str,
        message: str,
        instance_id: str | None = None,
        retain_for_debug: bool = False,
    ) -> ProvisioningFailureReport:
        """Create a failure report and clean up a partial VM when needed."""

        rollback = self._rollback(instance_id, retain_for_debug=retain_for_debug)
        return ProvisioningFailureReport(
            session_id=session_id,
            phase=phase,
            failed_step=failed_step,
            failure_class=failure_class or "unknown",
            message=message,
            instance_id=instance_id,
            rollback=rollback,
            timestamp=_utc_now(),
        )

    def _rollback(self, instance_id: str | None, *, retain_for_debug: bool) -> RollbackReport:
        timestamp = _utc_now()
        if not instance_id:
            return RollbackReport(
                action="none",
                status="not_needed",
                evidence="No instance id was created before failure.",
                timestamp=timestamp,
            )

        if retain_for_debug:
            return RollbackReport(
                action="retain_for_debug",
                status="retained",
                evidence=f"Retained instance '{instance_id}' by explicit debug request.",
                timestamp=timestamp,
            )

        try:
            state = self.adapter.get_instance_state(instance_id)
            if not state.exists:
                return RollbackReport(
                    action="none",
                    status="not_needed",
                    evidence=f"Instance '{instance_id}' is already absent.",
                    timestamp=timestamp,
                )
            destroy_status = self.adapter.destroy_instance(instance_id)
            return RollbackReport(
                action="destroy_partial_vm",
                status="destroyed",
                evidence=f"Destroyed partial instance '{instance_id}' with provider status '{destroy_status}'.",
                timestamp=timestamp,
            )
        except Exception as exc:
            return RollbackReport(
                action="destroy_partial_vm",
                status="failed",
                evidence=f"Rollback failed for instance '{instance_id}': {exc}",
                timestamp=timestamp,
            )
