"""Provider adapters for AVA v2 provisioning."""

from .base import ConnectionInfo, ProviderAdapter, ProviderState, ProvisioningPlan
from .virtualbox import VirtualBoxAdapter

__all__ = [
    "ConnectionInfo",
    "ProviderAdapter",
    "ProviderState",
    "ProvisioningPlan",
    "VirtualBoxAdapter",
]

