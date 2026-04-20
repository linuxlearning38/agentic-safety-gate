"""
AVA — control/incident_reporter.py
Incident Report Generator — Day 7, Phase 4

Generates a structured JSON report for every tool execution, graph run,
ReAct loop, and approved command. Reports are written to:
    AVA_REPORTS_DIR/YYYY-MM-DD/

Report filename:
    HH-MM-SS_<type>_<short_id>.json

Index file:
    AVA_REPORTS_DIR/index.json  (rolling, last 500 entries)
"""

import os
import json
import uuid
import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)

REPORTS_DIR = os.getenv("AVA_REPORTS_DIR", "/data/reports" if os.path.isdir("/data") else "/home/manoj/ava-data/reports")
INDEX_FILE  = os.path.join(REPORTS_DIR, "index.json")
INDEX_MAX   = 500


def _ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def _today_dir():
    d = os.path.join(REPORTS_DIR, date.today().isoformat())
    _ensure_dir(d)
    return d


def _load_index():
    try:
        if os.path.exists(INDEX_FILE):
            with open(INDEX_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[Reporter] Index load failed: {e}")
    return []


def _save_index(entries):
    try:
        _ensure_dir(REPORTS_DIR)
        entries = entries[-INDEX_MAX:]
        with open(INDEX_FILE, "w") as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        logger.warning(f"[Reporter] Index save failed: {e}")


def _write_report(report):
    try:
        day_dir  = _today_dir()
        ts       = datetime.now().strftime("%H-%M-%S")
        short_id = report["report_id"][:8]
        rtype    = report.get("type", "unknown")
        filepath = os.path.join(day_dir, f"{ts}_{rtype}_{short_id}.json")

        with open(filepath, "w") as f:
            json.dump(report, f, indent=2)

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
            f"[Reporter] Saved: type={rtype} "
            f"status={report.get('status')} "
            f"user={report.get('triggered_by')} "
            f"file={os.path.basename(filepath)}"
        )
        return filepath
    except Exception as e:
        logger.error(f"[Reporter] Write failed: {e}")
        return ""


def _base(rtype, triggered_by, ip_address, query, duration):
    return {
        "report_id":        str(uuid.uuid4()),
        "timestamp":        datetime.now().isoformat(),
        "date":             date.today().isoformat(),
        "type":             rtype,
        "triggered_by":     triggered_by or "unknown",
        "ip_address":       ip_address   or "unknown",
        "query":            (query or "")[:500],
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


def report_tool_execution(tool_name, tool_args, result,
                           triggered_by, ip_address, query="", duration=0.0):
    r = _base("tool", triggered_by, ip_address, query, duration)
    r.update({
        "tool_name":  tool_name,
        "risk_level": result.get("risk_level", "unknown"),
        "status":     result.get("status",     "unknown"),
        "output":     str(result.get("output", ""))[:2000],
        "error":      str(result.get("error",  ""))[:500],
        "metadata":   {"tool_args": tool_args},
    })
    return _write_report(r)


def report_graph_execution(graph_name, graph_result,
                            triggered_by, ip_address, query="", duration=0.0):
    r = _base("graph", triggered_by, ip_address, query, duration)

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

    steps = [
        {"tool": s.get("tool"), "status": s.get("status"),
         "output": str(s.get("output", ""))[:300]}
        for s in (graph_result.steps_run or [])
    ]

    r.update({
        "graph_name":      graph_name,
        "status":          status,
        "steps_completed": len(steps),
        "output":          (graph_result.summary_for_ui()[:2000]
                            if hasattr(graph_result, "summary_for_ui") else ""),
        "metadata": {
            "paused_at":   graph_result.paused_at,
            "approval_id": graph_result.approval_id,
            "steps":       steps,
        },
    })
    return _write_report(r)


def report_react_execution(react_result, triggered_by, ip_address,
                            query="", duration=0.0):
    r = _base("react", triggered_by, ip_address, query, duration)

    if react_result.success:
        status = "success"
    elif react_result.stopped_reason == "max_iterations":
        status = "max_iterations"
    else:
        status = "failure"

    tools_used = [
        {"iteration": s.iteration, "tool": s.action,
         "status": "success" if "SUCCESS" in str(s.observation) else "failure"}
        for s in (react_result.steps or [])
        if s.action and s.action not in ("final_answer", "none", "")
    ]

    r.update({
        "status":          status,
        "output":          str(react_result.final_answer)[:2000],
        "steps_completed": react_result.iterations,
        "steps_total":     5,
        "metadata": {
            "stopped_reason": react_result.stopped_reason,
            "tools_used":     tools_used,
        },
    })
    return _write_report(r)


def report_approved_execution(approval_id, result,
                               triggered_by, ip_address, duration=0.0):
    r = _base("approved", triggered_by, ip_address, "", duration)
    r.update({
        "status":   result.get("status", "unknown"),
        "output":   str(result.get("output", ""))[:2000],
        "error":    str(result.get("error",  ""))[:500],
        "metadata": {
            "approval_id": approval_id,
            "command":     str(result.get("command", "")),
        },
    })
    return _write_report(r)


def get_recent_reports(limit=20):
    index = _load_index()
    return list(reversed(index[-limit:]))


def get_report_by_id(report_id):
    for entry in _load_index():
        if entry.get("report_id") == report_id:
            fp = entry.get("filepath", "")
            if fp and os.path.exists(fp):
                try:
                    with open(fp) as f:
                        return json.load(f)
                except Exception as e:
                    logger.error(f"[Reporter] Load failed {report_id}: {e}")
    return None


def get_reports_stats():
    index = _load_index()
    if not index:
        return {"total": 0}
    by_type, by_status, by_user = {}, {}, {}
    for e in index:
        for d, k in [(by_type, "type"), (by_status, "status"),
                     (by_user, "triggered_by")]:
            v = e.get(k, "unknown")
            d[v] = d.get(v, 0) + 1
    return {
        "total":     len(index),
        "by_type":   by_type,
        "by_status": by_status,
        "by_user":   by_user,
        "oldest":    index[0].get("timestamp")  if index else None,
        "newest":    index[-1].get("timestamp") if index else None,
    }
