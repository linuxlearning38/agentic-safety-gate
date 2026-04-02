# control/__init__.py
# AgentGuard - Control System Package

"""
AgentGuard: Enterprise Security for AI Agents

This package provides:
- Command whitelist registry
- Approval queue management  
- Execution logging
- Security layer with risk analysis
- Threat pattern detection
- Secure command executor

Usage:
    from control.secure_executor import execute_command_secure
    result = execute_command_secure(cmd, query)
"""

__version__ = "1.0.0"
__author__ = "Manoj"

# Make key functions available at package level
from .secure_executor import execute_command_secure
from .registry import is_approved, add_to_whitelist
from .approval import add_request, get_pending
from .logger import log
from .security_layer import analyze_command_security

__all__ = [
    'execute_command_secure',
    'is_approved',
    'add_to_whitelist',
    'add_request',
    'get_pending',
    'log',
    'analyze_command_security'
]
