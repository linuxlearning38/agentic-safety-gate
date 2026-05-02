"""Desired-state model for the AVA v2 provisioning slice."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Dict, Iterable


ALLOWED_PROVIDERS = {"virtualbox"}
ALLOWED_OPERATING_SYSTEMS = {"ubuntu"}
ALLOWED_ROLES = {"web_server"}
ALLOWED_NETWORK_MODES = {"nat", "bridged", "hostonly"}
ALLOWED_FIREWALL_PROFILES = {"web_public", "internal_only"}
ALLOWED_HARDENING_PROFILES = {"baseline_linux", "none"}
REQUIRED_SPEC_FIELDS = ("cpu", "ram_gb", "disk_gb")
HOSTNAME_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"


class DesiredStateError(ValueError):
    """Raised when a desired state cannot be executed safely."""


def _coerce_positive_int(value: Any, field_name: str) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError) as exc:
        raise DesiredStateError(f"{field_name} must be a positive integer") from exc
    if coerced <= 0:
        raise DesiredStateError(f"{field_name} must be a positive integer")
    return coerced


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_hostname(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    normalized = raw.replace("_", "-")
    if not re.fullmatch(HOSTNAME_PATTERN, normalized):
        raise DesiredStateError(
            "hostname must start with a letter and contain only lowercase letters, numbers, and hyphens"
        )
    if normalized.endswith("-"):
        raise DesiredStateError("hostname must not end with a hyphen")
    return normalized


@dataclass(slots=True)
class DesiredState:
    """Execution contract for the first v2 provider slice."""

    provider: str = "virtualbox"
    os: str = "ubuntu"
    role: str = "web_server"
    cpu: int = 0
    ram_gb: int = 0
    disk_gb: int = 0
    network_mode: str = "nat"
    firewall_profile: str = "web_public"
    hardening_profile: str = "baseline_linux"
    post_login_actions: list[str] = field(default_factory=list)
    vm_name: str | None = None

    @classmethod
    def from_answers(cls, answers: Dict[str, Any]) -> "DesiredState":
        """Build a desired state from collected conversation answers."""

        normalized = {
            "provider": _normalize_text(answers.get("provider") or "virtualbox"),
            "os": _normalize_text(answers.get("os") or "ubuntu"),
            "role": _normalize_text(answers.get("role") or "web_server"),
            "cpu": _coerce_positive_int(answers.get("cpu"), "cpu"),
            "ram_gb": _coerce_positive_int(answers.get("ram_gb"), "ram_gb"),
            "disk_gb": _coerce_positive_int(answers.get("disk_gb"), "disk_gb"),
            "network_mode": _normalize_text(answers.get("network_mode") or "nat"),
            "firewall_profile": _normalize_text(answers.get("firewall_profile") or "web_public"),
            "hardening_profile": _normalize_text(answers.get("hardening_profile") or "baseline_linux"),
            "post_login_actions": list(answers.get("post_login_actions") or []),
            "vm_name": _normalize_hostname(answers.get("vm_name") or answers.get("hostname")),
        }
        desired = cls(**normalized)
        desired.validate()
        return desired

    def validate(self) -> None:
        """Validate v2.0.0 scope before any provider action is possible."""

        if self.provider not in ALLOWED_PROVIDERS:
            raise DesiredStateError("v2.0.0 only supports provider='virtualbox'")
        if self.os not in ALLOWED_OPERATING_SYSTEMS:
            raise DesiredStateError("v2.0.0 only supports os='ubuntu'")
        if self.role not in ALLOWED_ROLES:
            raise DesiredStateError("v2.0.0 only supports role='web_server'")
        if self.network_mode not in ALLOWED_NETWORK_MODES:
            raise DesiredStateError(f"network_mode must be one of {sorted(ALLOWED_NETWORK_MODES)}")
        if self.firewall_profile not in ALLOWED_FIREWALL_PROFILES:
            raise DesiredStateError(f"firewall_profile must be one of {sorted(ALLOWED_FIREWALL_PROFILES)}")
        if self.hardening_profile not in ALLOWED_HARDENING_PROFILES:
            raise DesiredStateError(f"hardening_profile must be one of {sorted(ALLOWED_HARDENING_PROFILES)}")
        _coerce_positive_int(self.cpu, "cpu")
        _coerce_positive_int(self.ram_gb, "ram_gb")
        _coerce_positive_int(self.disk_gb, "disk_gb")

    def to_dict(self) -> Dict[str, Any]:
        """Return a provider-friendly representation."""

        return asdict(self)


def missing_required_specs(answers: Dict[str, Any], required_fields: Iterable[str] = REQUIRED_SPEC_FIELDS) -> list[str]:
    """Return user-facing required spec fields that are still missing."""

    missing: list[str] = []
    for field_name in required_fields:
        value = answers.get(field_name)
        if value in (None, ""):
            missing.append(field_name)
            continue
        try:
            _coerce_positive_int(value, field_name)
        except DesiredStateError:
            missing.append(field_name)
    return missing
