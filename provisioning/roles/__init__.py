"""Role definitions for AVA v2 provisioning."""

from .base import BootstrapCommand, RoleDefinition
from .web_server import WEB_SERVER_ROLE, WebServerRole

__all__ = ["BootstrapCommand", "RoleDefinition", "WEB_SERVER_ROLE", "WebServerRole"]
