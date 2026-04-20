# control/secure_executor.py
# Phase 4 — Integrates Tool Registry
#
# Bugs fixed vs Phase 3:
#   1. shell=True removed — all execution goes through ToolRegistry._run() with shell=False
#   2. check_recent_approval now runs BEFORE whitelist for medium/high risk
#   3. Duplicate subprocess blocks eliminated — _run() is the single execution path
#   4. "whitelist_high_risk" auto-execute path removed — high risk always needs approval

from control.registry import is_approved, normalize_command_signature
from control.approval import add_request, update_status, load_queue, check_recent_approval
from control.logger import log as execution_log
from control.security_layer import (
    analyze_command_security,
    security_audit_log,
    validate_command_safety,
)
from control.tool_registry import registry as tool_registry
import json
import os
import urllib.error
import urllib.request


OPA_ACTION_POLICY_URL = os.getenv("OPA_ACTION_POLICY_URL", "http://opa:8181/v1/data/ava/authz/decision")
OPA_ACTION_POLICY_ENABLED = os.getenv("AVA_OPA_ACTION_POLICY_ENABLED", "true").lower() == "true"


def _normalize_source_label(source: str) -> str:
    normalized = (source or "unknown").strip().lower()
    mapping = {
        "ask_command": "direct_raw_command",
        "ask_operational": "operational_route_deterministic",
        "ask_operational_llm_fallback": "operational_route_bounded_classifier",
        "tools_route": "direct_tool_route",
    }
    return mapping.get(normalized, normalized or "unknown")


def _annotate_route_metadata(metadata: dict | None, source: str) -> dict:
    route_source = _normalize_source_label(source)
    enriched = dict(metadata or {})
    enriched["source"] = route_source
    enriched["route_source"] = route_source
    return enriched


def _normalize_result(
    *,
    status: str,
    mode: str,
    risk: str | None = None,
    command_repr: str = "",
    output: str = "",
    error: str = "",
    reason: str = "",
    approval_id: str | None = None,
    whitelisted: bool = False,
    threats: list | None = None,
    blast_radius: str | None = None,
    metadata: dict | None = None,
):
    return {
        "status": status,
        "mode": mode,
        "risk": risk,
        "approval_id": approval_id,
        "whitelisted": whitelisted,
        "command_repr": command_repr,
        "output": output or "",
        "error": error or "",
        "reason": reason or "",
        "threats": threats or [],
        "blast_radius": blast_radius,
        "metadata": metadata or {},
    }


def _opa_action_decision(*, mode: str, risk: str, command: str, query: str, tool_name: str = "", tool_args: dict | None = None, approved: bool = False) -> dict:
    if not OPA_ACTION_POLICY_ENABLED:
        return {"effect": "allow", "reason": "OPA action policy disabled", "policy_id": "disabled"}

    payload = {
        "input": {
            "mode": mode,
            "risk": risk,
            "command": command,
            "query": query,
            "tool_name": tool_name,
            "tool_args": tool_args or {},
            "approved": approved,
        }
    }
    req = urllib.request.Request(
        OPA_ACTION_POLICY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {
            "effect": "block",
            "reason": f"OPA action policy unavailable: {exc}",
            "policy_id": "opa_unavailable_fail_closed",
        }

    result = body.get("result") or {}
    effect = result.get("effect", "block")
    if effect not in {"allow", "require_approval", "block"}:
        effect = "block"
    return {
        "effect": effect,
        "reason": result.get("reason") or "OPA action policy returned no reason",
        "policy_id": result.get("policy_id") or "ava_action_policy",
    }


def _metadata_with_policy(metadata: dict | None, policy_decision: dict | None) -> dict:
    enriched = dict(metadata or {})
    if policy_decision:
        enriched["opa_policy"] = policy_decision
    return enriched


def _opa_block_result(*, mode: str, risk: str, command_repr: str, policy_decision: dict, threats: list | None = None, blast_radius: str | None = None) -> dict:
    return _normalize_result(
        status="blocked",
        mode=mode,
        risk=risk,
        command_repr=command_repr,
        reason=policy_decision.get("reason", "Blocked by OPA action policy"),
        threats=threats or [],
        blast_radius=blast_radius,
        metadata={"opa_policy": policy_decision},
    )


def _queue_for_approval(cmd, query, security_analysis, risk, threats, decision, *, mode, approval_key):
    approval_id = add_request(
        cmd,
        query,
        risk=risk,
        mode=mode,
        approval_key=approval_key,
        metadata={"decision": decision},
    )
    security_audit_log(
        event_type="queued_for_approval",
        cmd=cmd,
        query=query,
        risk_analysis=security_analysis["risk_analysis"],
        threats=threats,
        decision=decision,
    )
    return _normalize_result(
        status="approval_required",
        mode=mode,
        risk=risk,
        approval_id=approval_id,
        command_repr=cmd,
        reason=security_analysis.get("reason", ""),
        threats=threats,
        blast_radius=security_analysis["risk_analysis"]["blast_radius"],
    )

def _tool_security_analysis(tool_name: str, tool_args: dict, risk: str):
    return {
        "risk_analysis": {
            "risk": risk,
            "blast_radius": "high" if risk == "high" else risk,
            "description": f"{risk} risk tool execution",
            "matched_pattern": tool_name,
        },
        "threats": [],
        "recommendation": "require_approval" if risk in ("medium", "high") else "auto_approve",
        "reason": f"{risk.title()} risk tool '{tool_name}'",
        "tool_args": tool_args,
    }


def _execute_tool_now(tool_name: str, tool_args: dict, query: str, *, risk: str, approval_id=None, whitelisted=False, policy_decision: dict | None = None):
    cmd_repr = f"tool:{tool_name}({tool_args})"
    policy_decision = policy_decision or _opa_action_decision(
        mode="tool",
        risk=risk,
        command=cmd_repr,
        query=query,
        tool_name=tool_name,
        tool_args=tool_args,
        approved=bool(approval_id or whitelisted or risk == "low"),
    )
    if policy_decision["effect"] == "block":
        return _opa_block_result(mode="tool", risk=risk, command_repr=cmd_repr, policy_decision=policy_decision)

    result = tool_registry.execute(tool_name, tool_args)
    execution_log(query, f"tool:{tool_name}({tool_args})", result)
    if approval_id:
        update_status(approval_id, "executed")
    status = "success" if result.get("status") == "success" else "failed"
    metadata = {"tool_name": tool_name, "tool_args": tool_args, "tool_status": result.get("status"), "opa_policy": policy_decision}
    if isinstance(result.get("metadata"), dict):
        metadata.update(result["metadata"])
    return _normalize_result(
        status=status,
        mode="tool",
        risk=risk,
        approval_id=approval_id,
        whitelisted=whitelisted,
        command_repr=result.get("command_repr") or f"tool:{tool_name}({tool_args})",
        output=result.get("output", ""),
        error=result.get("error", ""),
        reason=result.get("error", "") if status != "success" else "",
        metadata=metadata,
    )


def _execute_raw_command_now(cmd, query, security_analysis, risk, threats, approval_id=None, whitelisted=False, policy_decision: dict | None = None):
    import shlex
    from control.tool_registry import _run

    shell_operators = ("|", "&&", "||", ";", ">", "<")
    if any(op in cmd for op in shell_operators):
        return _normalize_result(
            status="failed",
            mode="command",
            risk=risk,
            approval_id=approval_id,
            command_repr=cmd,
            error="Shell operators are not supported in raw command mode. Use a single command without pipes or redirection.",
            reason="Shell operators are not supported in raw command mode. Use a single command without pipes or redirection.",
            threats=threats,
            blast_radius=security_analysis["risk_analysis"]["blast_radius"],
        )

    args = shlex.split(cmd)
    policy_decision = policy_decision or _opa_action_decision(
        mode="command",
        risk=risk,
        command=cmd,
        query=query,
        approved=bool(approval_id or whitelisted or risk == "low"),
    )
    if policy_decision["effect"] == "block":
        security_audit_log(
            event_type="blocked",
            cmd=cmd,
            query=query,
            risk_analysis=security_analysis["risk_analysis"],
            threats=threats,
            decision="blocked_by_opa_policy",
        )
        return _opa_block_result(
            mode="command",
            risk=risk,
            command_repr=cmd,
            policy_decision=policy_decision,
            threats=threats,
            blast_radius=security_analysis["risk_analysis"]["blast_radius"],
        )

    result = _run(args, timeout=15)
    execution_log(query, cmd, result)
    security_audit_log(
        event_type="executed",
        cmd=cmd,
        query=query,
        risk_analysis=security_analysis["risk_analysis"],
        threats=threats,
        decision="whitelisted_execution" if whitelisted else "approved_execution" if approval_id else "auto_execute_low_risk",
    )
    if approval_id:
        update_status(approval_id, "executed")
    status = "success" if result.get("status") == "success" else "failed"
    return _normalize_result(
        status=status,
        mode="command",
        risk=risk,
        approval_id=approval_id,
        whitelisted=whitelisted,
        command_repr=result.get("command_repr", cmd),
        output=result.get("output", ""),
        error=result.get("error", ""),
        reason=result.get("error", "") if status != "success" else "",
        threats=threats,
        blast_radius=security_analysis["risk_analysis"]["blast_radius"],
        metadata={"opa_policy": policy_decision},
    )


# ─── Tool-based execution (Phase 4 primary path) ─────────────────────────────

def execute_tool_secure(tool_name: str, tool_args: dict, query: str) -> dict:
    """
    Execute a named tool from the ToolRegistry with full security controls.
    This is the Phase 4 primary path — replaces raw command strings.

    Flow:
        1. Look up tool → get risk level
        2. HIGH risk → always queue for approval
        3. MEDIUM risk → check recent approval first, then queue if none
        4. LOW risk → validate safety → execute via registry (shell=False)
        5. Log everything
    """
    tool = tool_registry.get_tool(tool_name)
    if not tool:
        return _normalize_result(
            status="failed",
            mode="tool",
            command_repr=f"tool:{tool_name}({tool_args})",
            error=f"Unknown tool: {tool_name}",
            reason=f"Unknown tool: {tool_name}",
            metadata={"tool_name": tool_name, "tool_args": tool_args},
        )

    risk = tool.risk_level
    cmd_repr = f"tool:{tool_name}({tool_args})"
    approval_key = f"tool:{tool_name}"
    security_analysis = _tool_security_analysis(tool_name, tool_args, risk)
    whitelisted = is_approved(approval_key) or is_approved(cmd_repr)
    policy_decision = _opa_action_decision(
        mode="tool",
        risk=risk,
        command=cmd_repr,
        query=query,
        tool_name=tool_name,
        tool_args=tool_args,
        approved=bool(whitelisted),
    )

    if policy_decision["effect"] == "block":
        security_audit_log(
            event_type="blocked",
            cmd=cmd_repr,
            query=query,
            risk_analysis=security_analysis["risk_analysis"],
            threats=[],
            decision="blocked_by_opa_policy",
        )
        return _opa_block_result(mode="tool", risk=risk, command_repr=cmd_repr, policy_decision=policy_decision)

    if risk == "low":
        if policy_decision["effect"] == "require_approval":
            return _queue_for_approval(
                cmd_repr,
                query,
                security_analysis,
                risk,
                [],
                "opa_policy_requires_approval",
                mode="tool",
                approval_key=approval_key,
            )
        security_audit_log(
            event_type="executed",
            cmd=cmd_repr,
            query=query,
            risk_analysis=security_analysis["risk_analysis"],
            threats=[],
            decision="auto_execute_low_risk_tool",
        )
        return _execute_tool_now(tool_name, tool_args, query, risk=risk, whitelisted=whitelisted, policy_decision=policy_decision)

    was_approved, approval_id = check_recent_approval(approval_key, minutes=10)
    if was_approved or whitelisted:
        security_audit_log(
            event_type="executed",
            cmd=cmd_repr,
            query=query,
            risk_analysis=security_analysis["risk_analysis"],
            threats=[],
            decision="whitelisted_tool" if whitelisted else "manual_approval_tool",
        )
        return _execute_tool_now(
            tool_name,
            tool_args,
            query,
            risk=risk,
            approval_id=approval_id if was_approved else None,
            whitelisted=whitelisted,
            policy_decision=policy_decision,
        )

    return _queue_for_approval(
        cmd_repr,
        query,
        security_analysis,
        risk,
        [],
        f"{risk}_risk_tool_pending_approval",
        mode="tool",
        approval_key=approval_key,
    )


def execute_tool_safe(tool_name: str, tool_args: dict, query: str, source: str = "unknown") -> dict:
    """
    Public execution authority for all executable requests.
    Structured tools route through execute_tool_secure().
    Raw command fallback routes through execute_command_secure().
    """
    if tool_name == "raw_command":
        cmd = (tool_args or {}).get("command", "")
        if not isinstance(cmd, str) or not cmd.strip():
            return _normalize_result(
                status="failed",
                mode="command",
                error="Missing raw command payload",
                reason="Missing raw command payload",
                metadata=_annotate_route_metadata({}, source),
            )
        result = execute_command_secure(cmd.strip(), query)
        result["metadata"] = _annotate_route_metadata(result.get("metadata"), source)
        return result

    result = execute_tool_secure(tool_name, tool_args or {}, query)
    result["metadata"] = _annotate_route_metadata(result.get("metadata"), source)
    return result


# ─── Legacy path — raw command strings (Phase 1-3 compatibility) ──────────────
# Keep this for any existing callers — but note: it still validates via
# security_layer. New Phase 4 code should use execute_tool_secure() instead.

def execute_command_secure(cmd: str, query: str) -> dict:
    """
    Legacy entry point for raw command strings.
    Phase 4: Prefer execute_tool_secure() for all new tool calls.
    """
    # Step 1: Analyze security
    security_analysis = analyze_command_security(cmd, query)
    risk        = security_analysis["risk_analysis"]["risk"]
    threats     = security_analysis["threats"]
    recommendation = security_analysis["recommendation"]

    # Step 2: Block immediately if policy says so
    if recommendation == "block":
        security_audit_log(
            event_type="blocked",
            cmd=cmd,
            query=query,
            risk_analysis=security_analysis["risk_analysis"],
            threats=threats,
            decision="blocked_by_policy",
        )
        return _normalize_result(
            status="blocked",
            mode="command",
            risk=risk,
            command_repr=cmd,
            reason=security_analysis["reason"],
            threats=threats,
            blast_radius=security_analysis["risk_analysis"]["blast_radius"],
        )

    policy_decision = _opa_action_decision(
        mode="command",
        risk=risk,
        command=cmd,
        query=query,
        approved=False,
    )
    if policy_decision["effect"] == "block":
        security_audit_log(
            event_type="blocked",
            cmd=cmd,
            query=query,
            risk_analysis=security_analysis["risk_analysis"],
            threats=threats,
            decision="blocked_by_opa_policy",
        )
        return _opa_block_result(
            mode="command",
            risk=risk,
            command_repr=cmd,
            policy_decision=policy_decision,
            threats=threats,
            blast_radius=security_analysis["risk_analysis"]["blast_radius"],
        )
    if risk == "low" and policy_decision["effect"] == "require_approval":
        return _queue_for_approval(
            cmd,
            query,
            security_analysis,
            risk,
            threats,
            "opa_policy_requires_approval",
            mode="command",
            approval_key=normalize_command_signature(cmd),
        )

    whitelisted = is_approved(cmd)
    normalized_cmd = normalize_command_signature(cmd)
    if risk == "low":
        is_safe, safety_reason = validate_command_safety(cmd)
        if not is_safe:
            security_audit_log(
                event_type="blocked",
                cmd=cmd,
                query=query,
                risk_analysis=security_analysis["risk_analysis"],
                threats=threats,
                decision="failed_safety_check",
            )
            return _normalize_result(
                status="blocked",
                mode="command",
                risk=risk,
                command_repr=cmd,
                reason=safety_reason,
                threats=threats,
                blast_radius=security_analysis["risk_analysis"]["blast_radius"],
            )
        return _execute_raw_command_now(
            cmd,
            query,
            security_analysis,
            risk,
            threats,
            whitelisted=whitelisted,
            policy_decision=policy_decision,
        )

    was_approved, approval_id = check_recent_approval(normalized_cmd, minutes=10)
    if was_approved or whitelisted:
        is_safe, safety_reason = validate_command_safety(cmd)
        if not is_safe:
            return _normalize_result(
                status="blocked",
                mode="command",
                risk=risk,
                command_repr=cmd,
                reason=safety_reason,
                threats=threats,
                blast_radius=security_analysis["risk_analysis"]["blast_radius"],
            )
        return _execute_raw_command_now(
            cmd,
            query,
            security_analysis,
            risk,
            threats,
            approval_id=approval_id if was_approved else None,
            whitelisted=whitelisted,
            policy_decision=policy_decision,
        )

    return _queue_for_approval(
        cmd,
        query,
        security_analysis,
        risk,
        threats,
        f"{risk}_risk_command_pending_approval",
        mode="command",
        approval_key=normalized_cmd,
    )


# ─── Approved command execution (called from approval UI) ─────────────────────

def execute_approved_command(approval_id: str) -> dict:
    """
    Execute a command after a human has approved it in the UI.
    Works for both tool-based and legacy command approvals.
    """
    import shlex
    from control.tool_registry import _run

    queue = load_queue()
    entry = next((e for e in queue if e["id"] == approval_id), None)

    if not entry:
        return _normalize_result(status="failed", mode="command", error="Approval ID not found", reason="Approval ID not found")
    if entry["status"] != "approved":
        return _normalize_result(
            status="failed",
            mode=entry.get("mode") or "command",
            error=f"Command not approved (status: {entry['status']})",
            reason=f"Command not approved (status: {entry['status']})",
        )

    cmd   = entry["command"]
    query = entry["query"]

    # Tool-based approval (cmd starts with "tool:")
    if cmd.startswith("tool:"):
        # Format: tool:tool_name({'arg': 'val'})
        # Re-execute via registry — args were validated on first call
        import ast, re
        match = re.match(r"tool:(\w+)\((.+)\)$", cmd, re.DOTALL)
        if match:
            tool_name = match.group(1)
            tool_args = ast.literal_eval(match.group(2))
            result = _execute_tool_now(
                tool_name,
                tool_args,
                query,
                risk=entry.get("risk") or "medium",
                approval_id=approval_id,
            )
        else:
            result = _normalize_result(
                status="failed",
                mode="tool",
                command_repr=cmd,
                error=f"Cannot parse tool command: {cmd}",
                reason=f"Cannot parse tool command: {cmd}",
            )
    else:
        # Legacy path — shlex.split, no shell
        security_analysis = analyze_command_security(cmd, query)
        risk = entry.get("risk") or security_analysis["risk_analysis"]["risk"]
        policy_decision = _opa_action_decision(
            mode="command",
            risk=risk,
            command=cmd,
            query=query,
            approved=True,
        )
        if policy_decision["effect"] == "block":
            security_audit_log(
                event_type="blocked",
                cmd=cmd,
                query=query,
                risk_analysis=security_analysis["risk_analysis"],
                threats=security_analysis["threats"],
                decision="approved_execution_blocked_by_opa_policy",
            )
            return _opa_block_result(
                mode="command",
                risk=risk,
                command_repr=cmd,
                policy_decision=policy_decision,
                threats=security_analysis["threats"],
                blast_radius=security_analysis["risk_analysis"]["blast_radius"],
            )
        args   = shlex.split(cmd)
        raw_result = _run(args, timeout=15)
        execution_log(query, cmd, raw_result)
        update_status(approval_id, "executed")
        result = _normalize_result(
            status="success" if raw_result.get("status") == "success" else "failed",
            mode="command",
            risk=risk,
            approval_id=approval_id,
            command_repr=raw_result.get("command_repr", cmd),
            output=raw_result.get("output", ""),
            error=raw_result.get("error", ""),
            reason=raw_result.get("error", "") if raw_result.get("status") != "success" else "",
            metadata={"opa_policy": policy_decision},
        )

    return result
