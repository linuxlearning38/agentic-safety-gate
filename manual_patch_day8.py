#!/usr/bin/env python3
"""
manual_patch_day8.py
Correct anchors extracted from your actual files.
Run from: /mnt/i/ai-lab/projects/devops-agent/
"""

from pathlib import Path
from datetime import datetime
import shutil

PROJECT_DIR = Path("/mnt/i/ai-lab/projects/devops-agent")
CONTROL_DIR = PROJECT_DIR / "control"
MAIN_APP    = PROJECT_DIR / "web_agent_v2.1_guardrail.py"
TOOL_REG    = CONTROL_DIR / "tool_registry.py"
SEC_EXEC    = CONTROL_DIR / "secure_executor.py"

OK  = "✅"
ERR = "❌"
INF = "ℹ️ "

def patch(path: Path, old: str, new: str, label: str) -> bool:
    content = path.read_text()
    if old not in content:
        if new.strip() in content:
            print(f"  {INF} {label} — already done")
            return True
        print(f"  {ERR} {label} — anchor not found")
        print(f"       Expected: {repr(old[:80])}")
        return False
    path.write_text(content.replace(old, new, 1))
    print(f"  {OK}  {label}")
    return True

# ─── 1. tool_registry.py ─────────────────────────────────────────────────────
# Real execute signature from your file: def execute(self, name: str, input_args: Dict) -> Dict:
# Real tool lookup from your file: tool = self._tools.get(name)

REGISTRY_NATIVE_METHOD = '''    def execute(self, name: str, input_args: Dict) -> Dict:'''

REGISTRY_NATIVE_METHOD_NEW = '''    def register_native(self, name: str, handler, description: str,
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

    def execute(self, name: str, input_args: Dict) -> Dict:'''

REGISTRY_DISPATCH_OLD = '''        tool = self._tools.get(name)'''

REGISTRY_DISPATCH_NEW = '''        tool = self._tools.get(name)
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
'''

# ─── 2. web_agent_v2.1_guardrail.py ─────────────────────────────────────────
# Real import line from your file (line 22): from control.incident_reporter import (

APP_IMPORT_OLD = '''from control.incident_reporter import ('''
APP_IMPORT_NEW = '''from control import vuln_scanner          # Day 8 — Trivy + Lynis
from control.incident_reporter import ('''

# Real startup anchor from your file (line 3210):
APP_STARTUP_OLD = '''    logger.info(f"Knowledge Base: {STATS[\'total_chunks\']} chunks")'''
APP_STARTUP_NEW = '''    logger.info(f"Knowledge Base: {STATS[\'total_chunks\']} chunks")

    # Day 8: Register vulnerability scanner tools
    for _vt in vuln_scanner.get_tool_descriptions():
        registry.register_native(
            name=_vt["name"],
            handler=_vt["handler"],
            description=_vt["description"],
            args=_vt["args"],
            risk_level=_vt["risk_level"],
            requires_approval=_vt["requires_approval"],
            available=_vt["available"],
        )
    _vt_avail = vuln_scanner.check_tools()
    logger.info(f"[VulnScanner] Trivy={_vt_avail[\'trivy\']} Lynis={_vt_avail[\'lynis\']}")'''

# For routes — find the last @app.route in reports section
# grep showed no REPORTS header, so we append before app.run or a known late route
# Using the /security route block which we know exists from Phase 1
APP_ROUTES_OLD = '''if __name__ == "__main__":'''
APP_ROUTES_NEW = '''# ═══════════════════════════ SCAN ROUTES (Day 8) ════════════════════

@app.route("/scan/check", methods=["GET"])
@jwt_required()
def route_scan_check():
    """GET /scan/check — Trivy + Lynis binary availability."""
    return jsonify({
        "tools": vuln_scanner.check_tools(),
        "install": {
            "trivy": "curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin",
            "lynis": "sudo apt install lynis",
        }
    })


@app.route("/scan/trivy", methods=["POST"])
@require_admin
@limiter.limit("5 per minute")
def route_scan_trivy():
    """POST /scan/trivy  {"image": "nginx:latest"}"""
    from flask_jwt_extended import get_jwt_identity
    body  = request.get_json(silent=True) or {}
    image = body.get("image", "").strip()
    if not image:
        return jsonify({"error": "Missing 'image' field"}), 400

    user   = get_jwt_identity()
    logger.info(f"[Scan] Trivy requested by {user}: {image}")
    result = vuln_scanner.scan_trivy(image)

    # Auto-report for critical/high findings
    if result.get("status") == "success" and result.get("risk_level") in ("critical", "high"):
        try:
            report_tool_execution(
                tool_name=f"trivy_scan",
                tool_args={"image": image},
                result=result,
                triggered_by=user,
                ip_address=request.remote_addr,
                duration=0,
            )
        except Exception as _re:
            logger.warning(f"[Scan] Auto-report failed: {_re}")

    return jsonify(result)


@app.route("/scan/lynis", methods=["POST"])
@require_admin
@limiter.limit("2 per minute")
def route_scan_lynis():
    """POST /scan/lynis  {} — runs Lynis system audit (requires sudo)"""
    from flask_jwt_extended import get_jwt_identity
    user   = get_jwt_identity()
    logger.info(f"[Scan] Lynis audit requested by {user}")
    result = vuln_scanner.scan_lynis()

    if result.get("status") == "success" and result.get("risk_level") in ("critical", "high"):
        try:
            report_tool_execution(
                tool_name="lynis_audit",
                tool_args={},
                result=result,
                triggered_by=user,
                ip_address=request.remote_addr,
                duration=0,
            )
        except Exception as _re:
            logger.warning(f"[Scan] Auto-report failed: {_re}")

    return jsonify(result)


if __name__ == "__main__":'''

# ─── 3. secure_executor.py ───────────────────────────────────────────────────
# Real anchor from your file (line 204):
SEC_OLD = '''    # Step 5: LOW risk — whitelist check + safety validation + execute'''
SEC_NEW = '''    # Step 5: LOW risk — whitelist check + safety validation + execute
    # Note: trivy/lynis are native tools — they never reach secure_executor'''

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  AVA Day 8 — Manual Patch (corrected anchors)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    results = []

    print("\n── tool_registry.py ─────────────────────────────────────────")
    results.append(patch(TOOL_REG, REGISTRY_NATIVE_METHOD, REGISTRY_NATIVE_METHOD_NEW,
                         "Add register_native() method"))
    results.append(patch(TOOL_REG, REGISTRY_DISPATCH_OLD, REGISTRY_DISPATCH_NEW,
                         "Add native dispatch in execute()"))

    print("\n── web_agent_v2.1_guardrail.py ──────────────────────────────")
    results.append(patch(MAIN_APP, APP_IMPORT_OLD, APP_IMPORT_NEW,
                         "Import vuln_scanner"))
    results.append(patch(MAIN_APP, APP_STARTUP_OLD, APP_STARTUP_NEW,
                         "Register scan tools at startup"))
    results.append(patch(MAIN_APP, APP_ROUTES_OLD, APP_ROUTES_NEW,
                         "Add /scan/* routes"))

    print("\n── secure_executor.py ───────────────────────────────────────")
    patch(SEC_EXEC, SEC_OLD, SEC_NEW, "Add trivy/lynis note to whitelist section")

    print("\n" + "=" * 60)
    failed = [r for r in results if r is False]
    if not failed:
        print(f"  {OK}  All patches applied.")
        print()
        print("  Next:")
        print("  1. fuser -k 5002/tcp && sleep 1 && python3 web_agent_v2.1_guardrail.py")
        print("  2. Check logs for: [VulnScanner] Trivy=... Lynis=...")
        print("  3. python3 test_day8.py --token $TOKEN")
    else:
        print(f"  {ERR}  {len(failed)} patch(es) failed — check anchors above")
    print("=" * 60)


if __name__ == "__main__":
    main()
