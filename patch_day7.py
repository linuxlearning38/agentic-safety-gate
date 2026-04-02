#!/usr/bin/env python3
"""
patch_day7.py — AVA Phase 4, Day 7: Incident Report Generator

What this patch does:
  1. Writes control/incident_reporter.py
  2. Creates /mnt/i/ai-lab/reports/ directory
  3. Patches web_agent_v2.1_guardrail.py:
       - Imports incident_reporter
       - Calls report_tool_execution()     after /tools/<n>/run
       - Calls report_approved_execution() after /execute_approved
       - Calls report_react_execution()    after /react/run
       - Calls report_graph_execution()    after execute_graph() in /ask
       - Calls report_react_execution()    after react_loop.run() in /ask
       - Adds GET /reports                 (list recent, admin only)
       - Adds GET /reports/<id>            (get full report, admin only)
       - Adds GET /reports/stats           (summary, admin only)

Run:
  python3 patch_day7.py
"""

import os
import sys
import shutil
from datetime import datetime

PROJECT_DIR  = os.path.dirname(os.path.abspath(__file__))
MAIN_FILE    = os.path.join(PROJECT_DIR, "web_agent_v2.1_guardrail.py")
REPORTS_DIR  = "/mnt/i/ai-lab/reports"

# ─────────────────────────────────────────────────────────────────────────────
# Patch strings
# ─────────────────────────────────────────────────────────────────────────────

# 1. Import incident_reporter after react_loop import
OLD_IMPORT = "from control.react_loop import react_loop"

NEW_IMPORT = """from control.react_loop import react_loop
from control.incident_reporter import (
    report_tool_execution,
    report_graph_execution,
    report_react_execution,
    report_approved_execution,
    get_recent_reports,
    get_report_by_id,
    get_reports_stats,
)"""

# 2. Patch /tools/<n>/run — add report after tool execution
#    Anchor: the return statement after tool_registry.execute()
OLD_TOOL_RETURN = """        logger.info(f"[Tool] Direct run: {tool_name}({tool_args})")
        result = tool_registry.execute(tool_name, tool_args)

        return jsonify({
            'tool':    tool_name,
            'status':  result.get('status'),
            'output':  result.get('output', ''),
            'error':   result.get('error', ''),
        })"""

NEW_TOOL_RETURN = """        logger.info(f"[Tool] Direct run: {tool_name}({tool_args})")
        _t0    = time.time()
        result = tool_registry.execute(tool_name, tool_args)
        _dur   = time.time() - _t0

        report_tool_execution(
            tool_name    = tool_name,
            tool_args    = tool_args,
            result       = result,
            triggered_by = get_jwt_identity(),
            ip_address   = request.remote_addr,
            duration     = _dur,
        )

        return jsonify({
            'tool':    tool_name,
            'status':  result.get('status'),
            'output':  result.get('output', ''),
            'error':   result.get('error', ''),
        })"""

# 3. Patch /execute_approved — add report after execution
OLD_EXEC_BLOCK = """        logger.info(f"[Approval] Executing approved command: {approval_id}")
        result = execute_approved_command(approval_id)

        if result.get('status') == 'executed':"""

NEW_EXEC_BLOCK = """        logger.info(f"[Approval] Executing approved command: {approval_id}")
        _t0    = time.time()
        result = execute_approved_command(approval_id)
        _dur   = time.time() - _t0

        report_approved_execution(
            approval_id  = approval_id,
            result       = result,
            triggered_by = get_jwt_identity(),
            ip_address   = request.remote_addr,
            duration     = _dur,
        )

        if result.get('status') == 'executed':"""

# 4. Patch /react/run — add report after react_loop.run()
OLD_REACT_BLOCK = """        logger.info(f"[ReAct Direct] Query: {query}")
        react_result = react_loop.run(query)

        return jsonify({"""

NEW_REACT_BLOCK = """        logger.info(f"[ReAct Direct] Query: {query}")
        _t0          = time.time()
        react_result = react_loop.run(query)
        _dur         = time.time() - _t0

        report_react_execution(
            react_result = react_result,
            triggered_by = get_jwt_identity(),
            ip_address   = request.remote_addr,
            query        = query,
            duration     = _dur,
        )

        return jsonify({"""

# 5. Patch execute_graph() call in /ask route
OLD_GRAPH_CALL = """            graph_result = execute_graph(graph_name, query)

            # If a medium-risk step needs approval, pause and tell the user"""

NEW_GRAPH_CALL = """            _graph_t0    = time.time()
            graph_result = execute_graph(graph_name, query)
            _graph_dur   = time.time() - _graph_t0

            # Report graph execution
            try:
                report_graph_execution(
                    graph_name   = graph_name,
                    graph_result = graph_result,
                    triggered_by = get_jwt_identity(),
                    ip_address   = request.remote_addr,
                    query        = query,
                    duration     = _graph_dur,
                )
            except Exception as _re:
                logger.warning(f"[Reporter] Graph report failed: {_re}")

            # If a medium-risk step needs approval, pause and tell the user"""

# 6. Patch react_loop.run() call in /ask route
OLD_REACT_IN_ASK = """                react_result = react_loop.run(query, initial_context=rag_context)"""

NEW_REACT_IN_ASK = """                _react_t0    = time.time()
                react_result = react_loop.run(query, initial_context=rag_context)
                _react_dur   = time.time() - _react_t0
                try:
                    report_react_execution(
                        react_result = react_result,
                        triggered_by = get_jwt_identity(),
                        ip_address   = request.remote_addr,
                        query        = query,
                        duration     = _react_dur,
                    )
                except Exception as _re:
                    logger.warning(f"[Reporter] ReAct report failed: {_re}")"""

# 7. New /reports endpoints — inject before error handlers
OLD_BEFORE_ERRORS = "@app.route('/rate-limit/status', methods=['GET'])\n@jwt_required()\ndef rate_limit_status():"

NEW_BEFORE_ERRORS = """# ── Day 7: Incident Report Endpoints ─────────────────────────────────────────

@app.route('/reports', methods=['GET'])
@require_admin
def list_reports():
    \"\"\"
    GET /reports?limit=20
    List recent incident reports (newest first). Admin only.
    \"\"\"
    try:
        limit   = min(int(request.args.get('limit', 20)), 100)
        reports = get_recent_reports(limit)
        return jsonify({
            'total':   len(reports),
            'reports': reports,
        })
    except Exception as e:
        logger.error(f"[Reports] list error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/reports/stats', methods=['GET'])
@require_admin
def reports_stats():
    \"\"\"
    GET /reports/stats
    Summary statistics across all reports. Admin only.
    \"\"\"
    try:
        return jsonify(get_reports_stats())
    except Exception as e:
        logger.error(f"[Reports] stats error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/reports/<report_id>', methods=['GET'])
@require_admin
def get_report(report_id):
    \"\"\"
    GET /reports/<report_id>
    Fetch a full report by ID. Admin only.
    \"\"\"
    try:
        report = get_report_by_id(report_id)
        if not report:
            return jsonify({'error': f'Report {report_id} not found'}), 404
        return jsonify(report)
    except Exception as e:
        logger.error(f"[Reports] get error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/rate-limit/status', methods=['GET'])
@jwt_required()
def rate_limit_status():"""

# ─────────────────────────────────────────────────────────────────────────────
PATCHES = [
    ("Import incident_reporter",         OLD_IMPORT,           NEW_IMPORT),
    ("/tools/<n>/run — add report",      OLD_TOOL_RETURN,      NEW_TOOL_RETURN),
    ("/execute_approved — add report",   OLD_EXEC_BLOCK,       NEW_EXEC_BLOCK),
    ("/react/run — add report",          OLD_REACT_BLOCK,      NEW_REACT_BLOCK),
    ("execute_graph in /ask — report",   OLD_GRAPH_CALL,       NEW_GRAPH_CALL),
    ("react_loop in /ask — report",      OLD_REACT_IN_ASK,     NEW_REACT_IN_ASK),
    ("/reports endpoints",               OLD_BEFORE_ERRORS,    NEW_BEFORE_ERRORS),
]
# ─────────────────────────────────────────────────────────────────────────────


def apply_patches():
    print("=" * 60)
    print("AVA — Day 7 Incident Report Generator Patch")
    print(f"Target: {MAIN_FILE}")
    print("=" * 60)

    if not os.path.exists(MAIN_FILE):
        print(f"❌  FATAL: {MAIN_FILE} not found.")
        sys.exit(1)

    # Create reports directory
    os.makedirs(REPORTS_DIR, exist_ok=True)
    print(f"\n✅  Reports dir: {REPORTS_DIR}")

    # Backup
    backup = MAIN_FILE + f".backup_day7_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(MAIN_FILE, backup)
    print(f"✅  Backup: {backup}")

    with open(MAIN_FILE) as f:
        content = f.read()

    failed = []
    print()
    for name, old, new in PATCHES:
        if old not in content:
            failed.append(name)
            print(f"  ⚠️  SKIP (anchor not found): {name}")
            continue
        content = content.replace(old, new, 1)
        print(f"  ✅  {name}")

    with open(MAIN_FILE, "w") as f:
        f.write(content)
    print(f"\n✅  Patched: {MAIN_FILE}")

    # Write incident_reporter.py
    _write_reporter_module()

    if failed:
        print(f"\n⚠️  {len(failed)} patch(es) skipped:")
        for n in failed:
            print(f"    - {n}")

    print("""
════════════════════════════════════════════════════════════

  Day 7 Patch Complete!

  NEXT STEPS:
  ──────────────────────────────────────────────────────────
  1. Restart AVA:
       fuser -k 5002/tcp && sleep 1
       python3 web_agent_v2.1_guardrail.py

  2. Trigger an execution (run any tool or ask a question)

  3. Check report was created:
       ls /mnt/i/ai-lab/reports/$(date +%Y-%m-%d)/
       cat /mnt/i/ai-lab/reports/index.json | python3 -m json.tool

  4. Test /reports endpoint:
       TOKEN=$(curl -s -X POST http://localhost:5002/auth/login \\
         -H "Content-Type: application/json" \\
         -d '{"username":"admin","password":"ava-admin-2026"}' \\
         | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

       curl -s http://localhost:5002/reports \\
         -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

       curl -s http://localhost:5002/reports/stats \\
         -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

════════════════════════════════════════════════════════════
""")


REPORTER_MODULE = open(
    os.path.join(os.path.dirname(__file__), "control", "incident_reporter.py")
    if os.path.exists(
        os.path.join(os.path.dirname(__file__), "control", "incident_reporter.py")
    ) else "/dev/null"
).read() if False else None  # loaded below


def _write_reporter_module():
    """Write the incident_reporter.py to control/ directory."""
    control_dir = os.path.join(PROJECT_DIR, "control")
    os.makedirs(control_dir, exist_ok=True)
    dest = os.path.join(control_dir, "incident_reporter.py")

    # The module content is embedded here so the patch is self-contained
    module_content = r'''"""
AVA — control/incident_reporter.py
Incident Report Generator — Day 7, Phase 4

Generates a structured JSON report for every tool execution, graph run,
ReAct loop, and approved command. Reports are written to:
    /mnt/i/ai-lab/reports/YYYY-MM-DD/

Report filename:
    HH-MM-SS_<type>_<short_id>.json

Index file:
    /mnt/i/ai-lab/reports/index.json  (rolling, last 500 entries)
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
'''
    with open(dest, "w") as f:
        f.write(module_content)
    print(f"✅  Written: {dest}")


if __name__ == "__main__":
    apply_patches()
