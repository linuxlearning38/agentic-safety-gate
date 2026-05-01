"""Base role contracts for AVA v2 provisioning."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class BootstrapCommand:
    """One role bootstrap command with expected failure classification."""

    name: str
    command: str
    timeout_seconds: int
    failure_class: str


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    """Static role contract used before any bootstrap command runs."""

    name: str
    packages: tuple[str, ...]
    services: tuple[str, ...]
    ports: tuple[str, ...]
    firewall_profile: str
    hardening_profile: str
    bootstrap_steps: tuple[BootstrapCommand, ...] = field(default_factory=tuple)
    verification_checks: tuple[BootstrapCommand, ...] = field(default_factory=tuple)
