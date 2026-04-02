#!/usr/bin/env python3
"""
patch_day4.py — AVA Phase 4 Day 4
Adds two new endpoints to web_agent_v2.1_guardrail.py:

  POST /execute_approved  — execute a command after UI approval
  GET  /tools             — list all registered tools with risk levels

Run from project root:
    python3 patch_day4.py
"""

import sys
import shutil
from pathlib import Path

TARGET = Path("web_agent_v2.1_guardrail.py")
BACKUP = Path("backups/phase3_close_20260401/web_agent_v2.1_guardrail_pre_day4.bak")

# ─── Patch 1: Import execute_approved_command ─────────────────────────────────

OLD_IMPORT = "from control.secure_executor import execute_command_secure"

NEW_IMPORT = """\
from control.secure_executor import execute_command_secure, execute_approved_command
from control.tool_registry import registry as tool_registry"""

# ─── Patch 2: New endpoints — insert before the error handlers ────────────────

OLD_ANCHOR = "@app.errorhandler(404)"

NEW_ENDPOINTS = '''\
@app.route('/execute_approved', methods=['POST'])
def execute_approved_route():
    """
    Execute a command that was previously queued for approval.
    Called from the UI approval panel.

    Body: {"approval_id": "abc123"}
    """
    try:
        data        = request.json
        approval_id = data.get('approval_id', '').strip()

        if not approval_id:
            return jsonify({'error': 'approval_id is required'}), 400

        logger.info(f"[Approval] Executing approved command: {approval_id}")
        result = execute_approved_command(approval_id)

        if result.get('status') == 'executed':
            return jsonify({
                'status':  'executed',
                'command': result.get('command', ''),
                'output':  result.get('output', {}),
            })
        else:
            return jsonify({
                'status': 'error',
                'error':  result.get('error', 'Unknown error'),
            }), 400

    except Exception as e:
        logger.error(f"Error in execute_approved: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/tools', methods=['GET'])
def list_tools_route():
    """
    List all registered tools with their risk levels and descriptions.
    Used by the UI to display available tools.
    """
    try:
        tools = tool_registry.list_tools()
        by_risk = {
            'low':    [t for t in tools if t['risk_level'] == 'low'],
            'medium': [t for t in tools if t['risk_level'] == 'medium'],
            'high':   [t for t in tools if t['risk_level'] == 'high'],
        }
        return jsonify({
            'total': len(tools),
            'by_risk': by_risk,
            'tools': tools,
        })
    except Exception as e:
        logger.error(f"Error listing tools: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/tools/<tool_name>/run', methods=['POST'])
def run_tool_route(tool_name):
    """
    Directly run a LOW risk tool from the UI.
    Medium/high risk tools go through the approval workflow.

    Body: {"args": {"namespace": "default", ...}}
    """
    try:
        data      = request.json or {}
        tool_args = data.get('args', {})

        tool = tool_registry.get_tool(tool_name)
        if not tool:
            return jsonify({'error': f"Tool '{tool_name}' not found"}), 404

        if tool.risk_level != 'low':
            return jsonify({
                'error':      f"Tool '{tool_name}' is {tool.risk_level} risk",
                'message':    'Use /ask to run medium/high risk tools through the approval workflow',
                'risk_level': tool.risk_level,
            }), 403

        logger.info(f"[Tool] Direct run: {tool_name}({tool_args})")
        result = tool_registry.execute(tool_name, tool_args)

        return jsonify({
            'tool':    tool_name,
            'status':  result.get('status'),
            'output':  result.get('output', ''),
            'error':   result.get('error', ''),
        })

    except Exception as e:
        logger.error(f"Error running tool {tool_name}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/react/run', methods=['POST'])
def react_run_route():
    """
    Directly trigger the ReAct loop for a query.
    Returns full trace including all iterations.

    Body: {"query": "my nginx pod is slow"}
    """
    try:
        data  = request.json
        query = data.get('query', '').strip()

        if not query:
            return jsonify({'error': 'query is required'}), 400

        logger.info(f"[ReAct Direct] Query: {query}")
        react_result = react_loop.run(query)

        return jsonify({
            'query':        query,
            'final_answer': react_result.final_answer,
            'iterations':   react_result.iterations,
            'stopped':      react_result.stopped_reason,
            'success':      react_result.success,
            'trace': [
                {
                    'iteration':    s.iteration,
                    'thought':      s.thought,
                    'action':       s.action,
                    'action_input': s.action_input,
                    'observation':  s.observation,
                    'final_answer': s.final_answer,
                }
                for s in react_result.steps
            ],
        })

    except Exception as e:
        logger.error(f"Error in react_run: {e}")
        return jsonify({'error': str(e)}), 500


@app.errorhandler(404)'''

# ─── Apply ────────────────────────────────────────────────────────────────────

def apply():
    if not TARGET.exists():
        print(f"❌  {TARGET} not found — run from project root")
        sys.exit(1)

    src = TARGET.read_text(encoding="utf-8")

    p1_needed = "execute_approved_command" not in src
    p2_needed = "/execute_approved" not in src

    if not p1_needed and not p2_needed:
        print("✅  Both patches already applied — nothing to do")
        sys.exit(0)

    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TARGET, BACKUP)
    print(f"📦  Backup saved: {BACKUP}")

    patched = src

    if p1_needed:
        if OLD_IMPORT not in patched:
            print("❌  Import anchor not found. Aborting.")
            sys.exit(1)
        patched = patched.replace(OLD_IMPORT, NEW_IMPORT, 1)
        print("✅  Patch 1 applied: execute_approved_command + tool_registry imports")

    if p2_needed:
        if OLD_ANCHOR not in patched:
            print("❌  Route anchor not found. Aborting.")
            sys.exit(1)
        patched = patched.replace(OLD_ANCHOR, NEW_ENDPOINTS, 1)
        print("✅  Patch 2 applied: 4 new endpoints added")

    TARGET.write_text(patched, encoding="utf-8")

    final = TARGET.read_text(encoding="utf-8")
    checks = [
        ("execute_approved import",   "execute_approved_command"),
        ("tool_registry import",      "from control.tool_registry import registry"),
        ("/execute_approved route",   "execute_approved_route"),
        ("/tools route",              "list_tools_route"),
        ("/tools/<tool>/run route",   "run_tool_route"),
        ("/react/run route",          "react_run_route"),
    ]
    print("\n── Verification ─────────────────────────────────────────────")
    all_ok = True
    for label, snippet in checks:
        ok = snippet in final
        all_ok = all_ok and ok
        print(f"  [{'✅' if ok else '❌'}] {label}")

    print()
    if all_ok:
        print("🎉  All patches verified. Restart AVA:")
        print("     fuser -k 5002/tcp && sleep 2 && python3 web_agent_v2.1_guardrail.py")
    else:
        print("❌  Verification failed — restoring backup")
        shutil.copy2(BACKUP, TARGET)
        sys.exit(1)

if __name__ == "__main__":
    apply()
