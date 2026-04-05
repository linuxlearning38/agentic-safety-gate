# control/tool_registry.py
# AVA Phase 4 — Day 1: Tool Registry
#
# Design principles:
#   1. shell=False ALWAYS — arg lists only, no string interpolation into shell
#   2. Tools are pre-defined functions, not raw command strings
#   3. Each tool validates its own inputs before building the arg list
#   4. Registry owns execution — secure_executor delegates here for tool calls
#   5. Standardised return: {"status", "output", "error", "command_repr"}

import subprocess
import shlex
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Any, List, Optional, Tuple
from datetime import datetime


# ─── Return type ──────────────────────────────────────────────────────────────

def _ok(output: str, command_repr: str) -> Dict:
    return {
        "status": "success",
        "output": output,
        "error": "",
        "command_repr": command_repr,
        "timestamp": datetime.now().isoformat(),
    }

def _fail(error: str, command_repr: str = "") -> Dict:
    return {
        "status": "failure",
        "output": "",
        "error": error,
        "command_repr": command_repr,
        "timestamp": datetime.now().isoformat(),
    }


# ─── Safe subprocess helper ───────────────────────────────────────────────────

def _run(args: List[str], timeout: int = 15) -> Dict:
    """
    Execute a command with shell=False.
    args must be a list — never a string.
    """
    command_repr = shlex.join(args)
    try:
        result = subprocess.run(
            args,
            shell=False,          # ← NEVER shell=True
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return _ok(result.stdout.strip() or "(no output)", command_repr)
        else:
            # Non-zero exit is a failure, not an exception
            return _fail(
                f"Exit {result.returncode}: {result.stderr.strip()}",
                command_repr,
            )
    except subprocess.TimeoutExpired:
        return _fail(f"Timed out after {timeout}s", command_repr)
    except FileNotFoundError:
        return _fail(f"Binary not found: {args[0]}", command_repr)
    except Exception as e:
        return _fail(str(e), command_repr)


# ─── Input validators ─────────────────────────────────────────────────────────

_SAFE_NAME   = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-\.]*$')   # pod/svc/node names
_SAFE_NS     = re.compile(r'^[a-z][a-z0-9\-]*$')                # k8s namespace
_SAFE_IMAGE  = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-\./\:@]*$')  # container image
_SAFE_SVC    = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-\.]*$')   # systemd service

def _validate(value: str, pattern: re.Pattern, label: str) -> Tuple[bool, str]:
    if not value or not isinstance(value, str):
        return False, f"{label} must be a non-empty string"
    if len(value) > 253:
        return False, f"{label} too long (max 253 chars)"
    if not pattern.match(value):
        return False, f"{label} '{value}' contains invalid characters"
    return True, ""


# ─── Tool implementations ─────────────────────────────────────────────────────
# Each function:
#   - Accepts a dict of validated kwargs
#   - Builds a List[str] arg list (never string interpolation into shell)
#   - Returns _ok() or _fail()

def _check_pod_status(args: Dict) -> Dict:
    namespace = args.get("namespace", "default")
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    return _run(["kubectl", "get", "pods", "-n", namespace, "-o", "wide"])


def _check_logs(args: Dict) -> Dict:
    pod_name  = args.get("pod_name", "")
    namespace = args.get("namespace", "default")
    lines     = args.get("lines", 50)

    ok, err = _validate(pod_name, _SAFE_NAME, "pod_name")
    if not ok:
        return _fail(err)
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    if not isinstance(lines, int) or not (1 <= lines <= 1000):
        return _fail("lines must be an integer between 1 and 1000")

    return _run([
        "kubectl", "logs", pod_name,
        "-n", namespace,
        "--tail", str(lines),
    ])


def _check_disk(args: Dict) -> Dict:
    return _run(["df", "-h"])


def _check_memory(args: Dict) -> Dict:
    return _run(["free", "-h"])


def _check_node_status(args: Dict) -> Dict:
    return _run(["kubectl", "get", "nodes", "-o", "wide"])


def _check_service_health(args: Dict) -> Dict:
    service = args.get("service", "")
    ok, err = _validate(service, _SAFE_SVC, "service")
    if not ok:
        return _fail(err)
    return _run(["systemctl", "status", service, "--no-pager"])


def _trivy_scan(args: Dict) -> Dict:
    image = args.get("image", "")
    ok, err = _validate(image, _SAFE_IMAGE, "image")
    if not ok:
        return _fail(err)
    return _run(["trivy", "image", "--no-progress", image], timeout=120)


def _check_pod_describe(args: Dict) -> Dict:
    pod_name  = args.get("pod_name", "")
    namespace = args.get("namespace", "default")
    ok, err = _validate(pod_name, _SAFE_NAME, "pod_name")
    if not ok:
        return _fail(err)
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    return _run(["kubectl", "describe", "pod", pod_name, "-n", namespace])


# ── MEDIUM RISK ───────────────────────────────────────────────────────────────

def _restart_pod(args: Dict) -> Dict:
    deployment = args.get("deployment", "")
    namespace  = args.get("namespace", "default")
    ok, err = _validate(deployment, _SAFE_NAME, "deployment")
    if not ok:
        return _fail(err)
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    return _run([
        "kubectl", "rollout", "restart",
        f"deployment/{deployment}",
        "-n", namespace,
    ])


def _scale_deployment(args: Dict) -> Dict:
    deployment = args.get("deployment", "")
    namespace  = args.get("namespace", "default")
    replicas   = args.get("replicas", None)

    ok, err = _validate(deployment, _SAFE_NAME, "deployment")
    if not ok:
        return _fail(err)
    ok, err = _validate(namespace, _SAFE_NS, "namespace")
    if not ok:
        return _fail(err)
    if not isinstance(replicas, int) or not (0 <= replicas <= 50):
        return _fail("replicas must be an integer between 0 and 50")

    return _run([
        "kubectl", "scale",
        f"deployment/{deployment}",
        f"--replicas={replicas}",
        "-n", namespace,
    ])


def _restart_service(args: Dict) -> Dict:
    service = args.get("service", "")
    ok, err = _validate(service, _SAFE_SVC, "service")
    if not ok:
        return _fail(err)
    return _run(["systemctl", "restart", service])


# ── HIGH RISK — always blocked here, approval flow in secure_executor ─────────

def _delete_service(args: Dict) -> Dict:
    # Should never reach execution — gated at HIGH risk in registry
    return _fail("delete_service requires manual approval and is never auto-executed")


def _drain_node(args: Dict) -> Dict:
    return _fail("drain_node requires manual approval and is never auto-executed")


def _apply_config(args: Dict) -> Dict:
    return _fail("apply_config requires manual approval and is never auto-executed")


# ─── Tool dataclass ───────────────────────────────────────────────────────────

@dataclass
class Tool:
    name:        str
    function:    Callable[[Dict], Dict]
    risk_level:  str           # "low" | "medium" | "high"
    description: str
    required_args: List[str]   = field(default_factory=list)
    optional_args: List[str]   = field(default_factory=list)


# ─── Registry ─────────────────────────────────────────────────────────────────

class ToolRegistry:
    """
    Central registry for all AVA tools.

    Usage:
        registry = ToolRegistry()
        result   = registry.execute("check_logs", {"pod_name": "nginx-pod"})
    """

    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        self._register_defaults()

    # ── Registration ──────────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        function: Callable,
        risk_level: str,
        description: str,
        required_args: Optional[List[str]] = None,
        optional_args: Optional[List[str]] = None,
    ) -> None:
        if risk_level not in ("low", "medium", "high"):
            raise ValueError(f"risk_level must be low/medium/high, got: {risk_level}")
        self._tools[name] = Tool(
            name=name,
            function=function,
            risk_level=risk_level,
            description=description,
            required_args=required_args or [],
            optional_args=optional_args or [],
        )

    def _register_defaults(self) -> None:
        # ── LOW RISK — auto-execute ────────────────────────────────────────────
        self.register(
            name="check_pod_status",
            function=_check_pod_status,
            risk_level="low",
            description="List all pods with status in a namespace",
            optional_args=["namespace"],
        )
        self.register(
            name="check_logs",
            function=_check_logs,
            risk_level="low",
            description="Fetch recent logs from a pod",
            required_args=["pod_name"],
            optional_args=["namespace", "lines"],
        )
        self.register(
            name="check_disk",
            function=_check_disk,
            risk_level="low",
            description="Show disk usage with df -h",
        )
        self.register(
            name="check_memory",
            function=_check_memory,
            risk_level="low",
            description="Show memory usage with free -h",
        )
        self.register(
            name="check_node_status",
            function=_check_node_status,
            risk_level="low",
            description="List all Kubernetes nodes with status",
        )
        self.register(
            name="check_service_health",
            function=_check_service_health,
            risk_level="low",
            description="Check systemd service status",
            required_args=["service"],
        )
        self.register(
            name="check_pod_describe",
            function=_check_pod_describe,
            risk_level="low",
            description="Describe a pod (events, limits, mounts)",
            required_args=["pod_name"],
            optional_args=["namespace"],
        )
        self.register(
            name="trivy_scan",
            function=_trivy_scan,
            risk_level="low",
            description="Scan a container image for CVEs with Trivy",
            required_args=["image"],
        )

        # ── MEDIUM RISK — approval required ───────────────────────────────────
        self.register(
            name="restart_pod",
            function=_restart_pod,
            risk_level="medium",
            description="Rollout restart a deployment",
            required_args=["deployment"],
            optional_args=["namespace"],
        )
        self.register(
            name="scale_deployment",
            function=_scale_deployment,
            risk_level="medium",
            description="Scale a deployment to N replicas",
            required_args=["deployment", "replicas"],
            optional_args=["namespace"],
        )
        self.register(
            name="restart_service",
            function=_restart_service,
            risk_level="medium",
            description="Restart a systemd service",
            required_args=["service"],
        )

        # ── HIGH RISK — always blocked, human approval only ───────────────────
        self.register(
            name="delete_service",
            function=_delete_service,
            risk_level="high",
            description="Delete a Kubernetes service (DESTRUCTIVE)",
            required_args=["service", "namespace"],
        )
        self.register(
            name="drain_node",
            function=_drain_node,
            risk_level="high",
            description="Drain a Kubernetes node (DESTRUCTIVE)",
            required_args=["node"],
        )
        self.register(
            name="apply_config",
            function=_apply_config,
            risk_level="high",
            description="Apply a Kubernetes config file (DESTRUCTIVE)",
            required_args=["filepath"],
        )

    # ── Execution ─────────────────────────────────────────────────────────────

    def register_native(self, name: str, handler, description: str,
                        args: dict, risk_level: str = "low",
                        requires_approval: bool = False,
                        available: bool = True):
        """Register a Python callable as a tool (not a shell command). Day 8."""
        self._tools[name] = type("Tool", (), {
            "name":              name,
            "description":       description,
            "args":              args,
            "risk_level":        risk_level,
            "type":              "native",
            "handler":           handler,
            "requires_approval": requires_approval,
            "available":         available,
        })()

    def execute(self, name: str, input_args: Dict) -> Dict:
        """
        Execute a registered tool.

        Returns standardised dict:
            {
                "status":       "success" | "failure" | "blocked" | "not_found",
                "output":       str,
                "error":        str,
                "command_repr": str,   # safe, loggable representation
                "risk_level":   str,
                "tool_name":    str,
                "timestamp":    str,
            }
        """
        tool = self._tools.get(name)
        # Day 8: native tool dispatch (Python callable, not shell)
        if tool and getattr(tool, "type", None) == "native":
            handler = getattr(tool, "handler", None)
            if not callable(handler):
                return {"status": "error", "error": f"Tool '{name}' has no callable handler", "tool_name": name}
            if not getattr(tool, "available", True):
                return {"status": "error", "error": f"Tool '{name}' unavailable (binary not installed)", "tool_name": name}
            try:
                result = handler(**input_args) if input_args else handler()
                if isinstance(result, dict):
                    result["tool_name"] = name
                return result
            except TypeError as e:
                return {"status": "error", "error": f"Tool '{name}' argument error: {e}", "tool_name": name}
            except Exception as e:
                return {"status": "error", "error": f"Tool '{name}' error: {e}", "tool_name": name}


        if not tool:
            return {
                "status":    "not_found",
                "output":    "",
                "error":     f"Tool '{name}' is not registered",
                "tool_name": name,
                "timestamp": datetime.now().isoformat(),
            }

        # High-risk tools are never auto-executed — always blocked here
        if tool.risk_level == "high":
            return {
                "status":    "blocked",
                "output":    "",
                "error":     (
                    f"Tool '{name}' is HIGH RISK and cannot be auto-executed. "
                    "Use the approval workflow."
                ),
                "risk_level": "high",
                "tool_name":  name,
                "timestamp":  datetime.now().isoformat(),
            }

        # Validate required args are present
        missing = [a for a in tool.required_args if a not in input_args]
        if missing:
            return {
                "status":    "failure",
                "output":    "",
                "error":     f"Missing required args for '{name}': {missing}",
                "tool_name": name,
                "timestamp": datetime.now().isoformat(),
            }

        # Call tool function
        result = tool.function(input_args)
        result["risk_level"] = tool.risk_level
        result["tool_name"]  = name
        return result

    # ── Introspection ─────────────────────────────────────────────────────────

    def get_tool(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> List[Dict]:
        return [
            {
                "name":          t.name,
                "risk_level":    t.risk_level,
                "description":   t.description,
                "required_args": t.required_args,
                "optional_args": t.optional_args,
            }
            for t in self._tools.values()
        ]

    def list_by_risk(self, risk_level: str) -> List[Dict]:
        return [t for t in self.list_tools() if t["risk_level"] == risk_level]


# ─── Singleton ────────────────────────────────────────────────────────────────
# Import this everywhere — one registry for the whole app

registry = ToolRegistry()


# ─── Quick test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    print("=== Registered tools ===")
    for t in registry.list_tools():
        print(f"  [{t['risk_level'].upper():6}] {t['name']:25} — {t['description']}")

    print("\n=== Injection test (should FAIL validation) ===")
    result = registry.execute("check_logs", {
        "pod_name": "nginx; rm -rf /",
        "namespace": "default",
    })
    print(json.dumps(result, indent=2))

    print("\n=== High-risk block test ===")
    result = registry.execute("delete_service", {
        "service": "nginx", "namespace": "default"
    })
    print(json.dumps(result, indent=2))

    print("\n=== Missing args test ===")
    result = registry.execute("check_logs", {})
    print(json.dumps(result, indent=2))

    print("\n=== Valid low-risk tool (dry check — kubectl may not be present) ===")
    result = registry.execute("check_disk", {})
    print(json.dumps(result, indent=2))
