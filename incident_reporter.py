"""
AVA — control/incident_reporter.py
Incident Report Generator — Day 7, Phase 4

Generates a structured JSON report for every tool execution, graph run,
ReAct loop, and approved command. Reports are written to:
    /mnt/i/ai-lab/reports/YYYY-MM-DD/

Report filename format:
    HH-MM-SS_<type>_<short_id>.json

Also maintains:
    /mnt/i/ai-lab/reports/index.json   ← rolling index of last 500 reports

Report schema:
    {
        "report_id":        "uuid4",
        "timestamp":        "ISO 8601",
        "date":             "YYYY-MM-DD",
        "type":             "tool|graph|react|approved",
        "triggered_by":     "username from JWT",
        "ip_address":       "client IP",
        "query":            "original user query (if any)",
        "tool_name":        "check_disk",
        "graph_name":       "pod_crashloop",
        "risk_level":       "low|medium|high|unknown",
        "status":           "success|failure|blocked|partial",
        "output":           "tool stdout",
        "error":            "error message if any",
        "steps_completed":  3,        # for graph/react
        "steps_total":      5,
        "duration_seconds": 1.23,
        "metadata":         {}        # extra context
    }
"""

import os
import json
import uuid
import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)

REPORTS_DIR = "/mnt/i/ai-lab/reports"
INDEX_FILE  = os.path.join(REPORTS_DIR, "index.json")
INDEX_MAX   = 500   # keep last N entries in index


# ── Internal helpers ──────────────────────────────────────────────────────────

def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def _today_dir() -> str:
    d = os.path.join(REPORTS_DIR, date.today().isoformat())
    _ensure_dir(d)
    return d


def _load_index() -> list:
    try:
        if os.path.exists(INDEX_FILE):
            with open(INDEX_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[Reporter] Index load failed: {e}")
    return []


def _save_index(entries: list):
    try:
        _ensure_dir(REPORTS_DIR)
        # Keep only last INDEX_MAX entries
        entries = entries[-INDEX_MAX:]
        with open(INDEX_FILE, "w") as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        logger.warning(f"[Reporter] Index save failed: {e}")


def _write_report(report: dict) -> str:
    """Write report JSON to disk. Returns file path."""
    try:
        day_dir   = _today_dir()
        ts        = datetime.now().strftime("%H-%M-%S")
        short_id  = report["report_id"][:8]
        rtype     = report.get("type", "unknown")
        filename  = f"{ts}_{rtype}_{short_id}.json"
        filepath  = os.path.join(day_dir, filename)

        with open(filepath, "w") as f:
            json.dump(report, f, indent=2)

        # Update rolling index
        index = _load_index()
        index.append({
            "report_id":    report["report_id"],
            "timestamp":    report["timestamp"],
            "type":         report.get("type"),
            "triggered_by": report.get("triggered_by"),
            "tool_name":    report.get("tool_name"),
            "graph_name":   report.get("graph_name"),
            "risk_level":   report.get("risk_level"),
            "status":       report.get("status"),
            "filepath":     filepath,
        })
        _save_index(index)

        logger.info(
            f"[Reporter] Report saved: type={rtype} "
            f"status={report.get('status')} "
            f"user={report.get('triggered_by')} "
            f"file={filename}"
        )
        return filepath

    except Exception as e:
        logger.error(f"[Reporter] Failed to write report: {e}")
        return ""


def _base_report(
    rtype:        str,
    triggered_by: str,
    ip_address:   str,
    query:        str,
    duration:     float,
) -> dict:
    return {
        "report_id":        str(uuid.uuid4()),
        "timestamp":        datetime.now().isoformat(),
        "date":             date.today().isoformat(),
        "type":             rtype,
        "triggered_by":     triggered_by or "unknown",
        "ip_address":       ip_address   or "unknown",
        "query":            query[:500]  if query else "",
        "tool_name":        None,
        "graph_name":       None,
        "risk_level":       "unknown",
        "status":           "unknown",
        "output":           "",
        "error":            "",
        "steps_completed":  None,
        "steps_total":      None,
        "duration_seconds": round(duration, 3),
        "metadata":         {},
    }


# ── Public API ────────────────────────────────────────────────────────────────

def report_tool_execution(
    tool_name:    str,
    tool_args:    dict,
    result:       dict,
    triggered_by: str,
    ip_address:   str,
    query:        str = "",
    duration:     float = 0.0,
) -> str:
    """
    Generate report for a direct tool execution (/tools/<n>/run).
    Called after tool_registry.execute().
    """
    report = _base_report("tool", triggered_by, ip_address, query, duration)
    report.update({
        "tool_name":  tool_name,
        "risk_level": result.get("risk_level", "unknown"),
        "status":     result.get("status", "unknown"),
        "output":     str(result.get("output", ""))[:2000],
        "error":      str(result.get("error",  ""))[:500],
        "metadata":   {"tool_args": tool_args},
    })
    return _write_report(report)


def report_graph_execution(
    graph_name:   str,
    graph_result,          # GraphResult object
    triggered_by: str,
    ip_address:   str,
    query:        str = "",
    duration:     float = 0.0,
) -> str:
    """
    Generate report for a command graph execution.
    Called after execute_graph().
    """
    report = _base_report("graph", triggered_by, ip_address, query, duration)

    # Determine overall status
    if graph_result.paused_at:
        status = "paused_for_approval"
    elif graph_result.steps_run and all(
        s.get("status") == "success" for s in graph_result.steps_run
    ):
        status = "success"
    elif graph_result.steps_run:
        status = "partial"
    else:
        status = "failure"

    # Collect step outputs
    steps_output = []
    for step in (graph_result.steps_run or []):
        steps_output.append({
            "tool":   step.get("tool"),
            "status": step.get("status"),
            "output": str(step.get("output", ""))[:500],
        })

    report.update({
        "graph_name":      graph_name,
        "status":          status,
        "steps_completed": len(graph_result.steps_run or []),
        "output":          graph_result.summary_for_ui()[:2000] if hasattr(graph_result, "summary_for_ui") else "",
        "metadata": {
            "paused_at":   graph_result.paused_at,
            "approval_id": graph_result.approval_id,
            "steps":       steps_output,
        },
    })
    return _write_report(report)


def report_react_execution(
    react_result,          # ReActResult object
    triggered_by: str,
    ip_address:   str,
    query:        str = "",
    duration:     float = 0.0,
) -> str:
    """
    Generate report for a ReAct loop execution.
    Called after react_loop.run().
    """
    report = _base_report("react", triggered_by, ip_address, query, duration)

    status = "success" if react_result.success else (
        "max_iterations" if react_result.stopped_reason == "max_iterations" else "failure"
    )

    # Collect tools used across iterations
    tools_used = []
    for step in (react_result.steps or []):
        if step.action and step.action not in ("final_answer", "none", ""):
            tools_used.append({
                "iteration": step.iteration,
                "tool":      step.action,
                "status":    "success" if "SUCCESS" in str(step.observation) else "failure",
            })

    report.update({
        "status":          status,
        "output":          str(react_result.final_answer)[:2000],
        "steps_completed": react_result.iterations,
        "steps_total":     5,   # max iterations
        "metadata": {
            "stopped_reason": react_result.stopped_reason,
            "tools_used":     tools_used,
        },
    })
    return _write_report(report)


def report_approved_execution(
    approval_id:  str,
    result:       dict,
    triggered_by: str,
    ip_address:   str,
    duration:     float = 0.0,
) -> str:
    """
    Generate report for a manually approved command execution.
    Called after execute_approved_command().
    """
    report = _base_report("approved", triggered_by, ip_address, "", duration)
    report.update({
        "status":    result.get("status", "unknown"),
        "output":    str(result.get("output", ""))[:2000],
        "error":     str(result.get("error",  ""))[:500],
        "metadata":  {
            "approval_id": approval_id,
            "command":     str(result.get("command", "")),
        },
    })
    return _write_report(report)


# ── Query functions (for /reports endpoint) ───────────────────────────────────

def get_recent_reports(limit: int = 20) -> list:
    """Return most recent N reports from index (newest first)."""
    index = _load_index()
    return list(reversed(index[-limit:]))


def get_report_by_id(report_id: str) -> Optional[dict]:
    """Load a full report by its ID. Searches index for filepath."""
    index = _load_index()
    for entry in index:
        if entry.get("report_id") == report_id:
            filepath = entry.get("filepath", "")
            if filepath and os.path.exists(filepath):
                try:
                    with open(filepath) as f:
                        return json.load(f)
                except Exception as e:
                    logger.error(f"[Reporter] Failed to load report {report_id}: {e}")
    return None


def get_reports_stats() -> dict:
    """Summary stats from the index."""
    index = _load_index()
    if not index:
        return {"total": 0}

    by_type   = {}
    by_status = {}
    by_user   = {}

    for e in index:
        t = e.get("type",    "unknown")
        s = e.get("status",  "unknown")
        u = e.get("triggered_by", "unknown")
        by_type[t]   = by_type.get(t, 0)   + 1
        by_status[s] = by_status.get(s, 0) + 1
        by_user[u]   = by_user.get(u, 0)   + 1

    return {
        "total":     len(index),
        "by_type":   by_type,
        "by_status": by_status,
        "by_user":   by_user,
        "oldest":    index[0].get("timestamp")  if index else None,
        "newest":    index[-1].get("timestamp") if index else None,
    }
