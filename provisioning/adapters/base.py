"""Base contract for AVA v2 provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(slots=True)
class ProvisioningPlan:
    """Structured provider plan emitted before VM creation."""

    provider: str
    vm_name: str
    image: str
    cpu_count: int
    memory_mb: int
    disk_gb: int
    network_mode: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConnectionInfo:
    """Access details AVA will later hand back to the user."""

    username: str
    host: str
    port: int = 22
    temporary_password: str | None = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderState:
    """Provider-visible state for a provisioned instance."""

    instance_id: str
    exists: bool
    power_state: str
    provider_status: str
    raw: Dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(ABC):
    """Abstract contract every v2 provider adapter must implement."""

    provider_name: str = "unknown"

    @abstractmethod
    def plan_instance(self, desired_state: Dict[str, Any]) -> ProvisioningPlan:
        """Build a structured provider plan from the desired state."""

    @abstractmethod
    def create_instance(self, plan: ProvisioningPlan) -> str:
        """Create a provider instance and return its provider-specific id."""

    @abstractmethod
    def start_instance(self, instance_id: str) -> str:
        """Start a provider instance and return the resulting provider status."""

    @abstractmethod
    def stop_instance(self, instance_id: str) -> str:
        """Stop a provider instance and return the resulting provider status."""

    @abstractmethod
    def destroy_instance(self, instance_id: str) -> str:
        """Destroy a provider instance and return the resulting provider status."""

    @abstractmethod
    def configure_network(self, instance_id: str, network_spec: Dict[str, Any]) -> str:
        """Apply provider-side network configuration for the instance."""

    @abstractmethod
    def inject_access(self, instance_id: str, access_spec: Dict[str, Any]) -> str:
        """Inject credentials or access material after provisioning."""

    @abstractmethod
    def get_instance_state(self, instance_id: str) -> ProviderState:
        """Return provider-visible state for the instance."""

    @abstractmethod
    def get_connection_info(self, instance_id: str) -> ConnectionInfo:
        """Return connection information for the instance."""

