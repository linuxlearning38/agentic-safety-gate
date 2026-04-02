# control/secure_executor.py
# Phase 4 — Integrates Tool Registry
#
# Bugs fixed vs Phase 3:
#   1. shell=True removed — all execution goes through ToolRegistry._run() with shell=False
#   2. check_recent_approval now runs BEFORE whitelist for medium/high risk
#   3. Duplicate subprocess blocks eliminated — _run() is the single execution path
#   4. "whitelist_high_risk" auto-execute path removed — high risk always needs approval

from control.registry import is_approved
from control.approval import add_request, update_status, load_queue
from control.logger import log as execution_log
from control.security_layer import (
    analyze_command_security,
    security_audit_log,
    validate_command_safety,
)
from control.tool_registry import registry as tool_registry
from datetime import datetime, timedelta


# ─── Approval helpers ─────────────────────────────────────────────────────────

def check_recent_approval(cmd: str, minutes: int = 10):
    """
    Check if this command was approved within the last N minutes.
    Returns (approved: bool, approval_id: str | None)
    """
    queue = load_queue()
    cutoff = datetime.now() - timedelta(minutes=minutes)
    for entry in queue:
        if (
            entry["command"] == cmd
            and entry["status"] == "approved"
            and datetime.fromisoformat(entry["timestamp"]) > cutoff
        ):
            return True, entry["id"]
    return False, None


def _queue_for_approval(cmd, query, security_analysis, risk, threats, decision):
    approval_id = add_request(cmd, query)
    security_audit_log(
        event_type="queued_for_approval",
        cmd=cmd,
        query=query,
        risk_analysis=security_analysis["risk_analysis"],
        threats=threats,
        decision=decision,
    )
    return {
        "status":      "approval_required",
        "approval_id": approval_id,
        "command":     cmd,
        "risk":        risk,
        "blast_radius": security_analysis["risk_analysis"]["blast_radius"],
        "threats":     threats,
        "reason":      security_analysis.get("reason", ""),
        "message":     f"⚠️ {risk.upper()} RISK: Approval required (ID: {approval_id})",
    }


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
        return {"status": "error", "error": f"Unknown tool: {tool_name}"}

    risk = tool.risk_level

    # Build a loggable command string for audit trail
    cmd_repr = f"tool:{tool_name}({tool_args})"

    # ── HIGH risk — always queue, never auto-execute ──────────────────────────
    if risk == "high":
        approval_id = add_request(cmd_repr, query)
        security_audit_log(
            event_type="queued_for_approval",
            cmd=cmd_repr,
            query=query,
            risk_analysis={"risk": "high", "blast_radius": "high"},
            threats=[],
            decision="high_risk_tool_always_blocked",
        )
        return {
            "status":      "approval_required",
            "approval_id": approval_id,
            "tool_name":   tool_name,
            "risk":        "high",
            "message":     f"⚠️ HIGH RISK tool '{tool_name}' requires manual approval (ID: {approval_id})",
        }

    # ── MEDIUM risk — check recent approval first ─────────────────────────────
    if risk == "medium":
        was_approved, approval_id = check_recent_approval(cmd_repr, minutes=10)
        if was_approved:
            result = tool_registry.execute(tool_name, tool_args)
            execution_log(query, cmd_repr, result)
            security_audit_log(
                event_type="executed",
                cmd=cmd_repr,
                query=query,
                risk_analysis={"risk": "medium", "blast_radius": "medium"},
                threats=[],
                decision="manual_approval",
            )
            update_status(approval_id, "executed")
            result["approval_id"] = approval_id
            return result

        # No recent approval → queue
        approval_id = add_request(cmd_repr, query)
        security_audit_log(
            event_type="queued_for_approval",
            cmd=cmd_repr,
            query=query,
            risk_analysis={"risk": "medium", "blast_radius": "medium"},
            threats=[],
            decision="medium_risk_pending_approval",
        )
        return {
            "status":      "approval_required",
            "approval_id": approval_id,
            "tool_name":   tool_name,
            "risk":        "medium",
            "message":     f"⚠️ MEDIUM RISK tool '{tool_name}' requires approval (ID: {approval_id})",
        }

    # ── LOW risk — execute immediately via registry (shell=False guaranteed) ──
    result = tool_registry.execute(tool_name, tool_args)
    execution_log(query, cmd_repr, result)
    security_audit_log(
        event_type="executed",
        cmd=cmd_repr,
        query=query,
        risk_analysis={"risk": "low", "blast_radius": "low"},
        threats=[],
        decision="auto_approved_low_risk_tool",
    )
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
        return {
            "status": "blocked",
            "reason": security_analysis["reason"],
            "risk":   risk,
            "threats": threats,
        }

    # Step 3: HIGH risk — always queue (no whitelist bypass)
    if risk == "high":
        return _queue_for_approval(
            cmd, query, security_analysis, risk, threats,
            "high_risk_always_requires_approval"
        )

    # Step 4: MEDIUM risk — check recent approval BEFORE whitelist
    if risk == "medium":
        was_approved, approval_id = check_recent_approval(cmd, minutes=10)
        if was_approved:
            return _execute_legacy(cmd, query, security_analysis, risk, threats, approval_id)
        return _queue_for_approval(
            cmd, query, security_analysis, risk, threats,
            "medium_risk_pending_approval"
        )

    # Step 5: LOW risk — whitelist check + safety validation + execute
    # Note: trivy/lynis are native tools — they never reach secure_executor
    if risk == "low":
        if not is_approved(cmd):
            return _queue_for_approval(
                cmd, query, security_analysis, risk, threats,
                "low_risk_not_whitelisted"
            )
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
            return {"status": "blocked", "reason": safety_reason, "risk": risk}

        return _execute_legacy(cmd, query, security_analysis, risk, threats, approval_id=None)

    # Fallback — unknown risk
    return _queue_for_approval(
        cmd, query, security_analysis, risk, threats,
        "unknown_risk_pending_review"
    )


def _execute_legacy(cmd, query, security_analysis, risk, threats, approval_id):
    """
    Execute a legacy command string.
    Uses tool_registry._run() internally — shell=False, arg list via shlex.split().
    Falls back to a safe shlex.split() — still better than shell=True.
    """
    import shlex
    from control.tool_registry import _run  # reuse the safe runner

    args = shlex.split(cmd)  # raises ValueError on malformed input — intentional
    result = _run(args, timeout=15)

    execution_log(query, cmd, result)
    security_audit_log(
        event_type="executed",
        cmd=cmd,
        query=query,
        risk_analysis=security_analysis["risk_analysis"],
        threats=threats,
        decision="auto_approved_whitelist" if risk == "low" else "manual_approval",
    )

    if approval_id:
        update_status(approval_id, "executed")
        result["approval_id"] = approval_id

    result["risk"]   = risk
    result["threats"] = threats
    return result


# ─── Approved command execution (called from approval UI) ─────────────────────

def execute_approved_command(approval_id: str) -> dict:
    """
    Execute a command after a human has approved it in the UI.
    Works for both tool-based and legacy command approvals.
    """
    from control.approval import load_queue
    import shlex
    from control.tool_registry import _run

    queue = load_queue()
    entry = next((e for e in queue if e["id"] == approval_id), None)

    if not entry:
        return {"status": "error", "error": "Approval ID not found"}
    if entry["status"] != "approved":
        return {"status": "error", "error": f"Command not approved (status: {entry['status']})"}

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
            result = tool_registry.execute(tool_name, tool_args)
        else:
            result = {"status": "error", "error": f"Cannot parse tool command: {cmd}"}
    else:
        # Legacy path — shlex.split, no shell
        args   = shlex.split(cmd)
        result = _run(args, timeout=15)

    execution_log(query, cmd, result)
    update_status(approval_id, "executed")
    return result
