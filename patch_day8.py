#!/usr/bin/env python3
"""
patch_day8.py
AVA Phase 4 — Day 8: Trivy + Lynis Integration

What this does:
  1. Copies control/vuln_scanner.py into place
  2. Patches tool_registry.py to support "native" (Python handler) tools
  3. Patches web_agent_v2.1_guardrail.py:
     - Imports vuln_scanner at startup
     - Registers scan tools on boot
     - Adds /scan/trivy and /scan/lynis convenience routes
     - Wires scan results into incident_reporter
  4. Patches secure_executor.py whitelist (trivy read-only commands)
  5. Installs dependencies (if missing)

Run:
  cd /mnt/i/ai-lab/projects/devops-agent/
  source venv/bin/activate
  python3 patch_day8.py
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

# ─── Config ──────────────────────────────────────────────────────────────────

PROJECT_DIR = Path("/mnt/i/ai-lab/projects/devops-agent")
CONTROL_DIR = PROJECT_DIR / "control"
MAIN_APP    = PROJECT_DIR / "web_agent_v2.1_guardrail.py"
TOOL_REG    = CONTROL_DIR / "tool_registry.py"
SEC_EXEC    = CONTROL_DIR / "secure_executor.py"
VULN_SCAN   = CONTROL_DIR / "vuln_scanner.py"
PATCH_SCRIPT = Path(__file__).parent / "control" / "vuln_scanner.py"

BACKUP_DIR = PROJECT_DIR / f"backups/day8_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

OK  = "✅"
ERR = "❌"
INF = "ℹ️ "


# ─── Helpers ─────────────────────────────────────────────────────────────────

def backup(path: Path):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / path.name
    shutil.copy2(path, dst)
    print(f"  {INF} Backed up {path.name} → {dst}")


def patch_file(path: Path, old: str, new: str, label: str) -> bool:
    content = path.read_text()
    if old not in content:
        if new.strip() in content:
            print(f"  {INF} {label} — already patched, skipping")
            return True
        print(f"  {ERR} {label} — anchor not found in {path.name}")
        print(f"       Looking for: {repr(old[:80])}")
        return False
    patched = content.replace(old, new, 1)
    path.write_text(patched)
    print(f"  {OK}  {label}")
    return True


def run(cmd: list, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


# ─── Step 1: Copy vuln_scanner.py ────────────────────────────────────────────

def step1_copy_scanner():
    print("\n── Step 1: Install vuln_scanner.py ──────────────────────────────")
    src = PATCH_SCRIPT
    if not src.exists():
        # If running from project dir, look next to patch script
        src = Path(__file__).parent / "vuln_scanner.py"
    if not src.exists():
        print(f"  {ERR} vuln_scanner.py not found at {src}")
        print(f"       Copy it to control/ manually then re-run.")
        sys.exit(1)

    CONTROL_DIR.mkdir(exist_ok=True)
    if VULN_SCAN.exists():
        backup(VULN_SCAN)
    if src.resolve() != VULN_SCAN.resolve(): shutil.copy2(src, VULN_SCAN)
    print(f"  {OK}  Copied vuln_scanner.py → {VULN_SCAN}")


# ─── Step 2: Patch tool_registry.py ─────────────────────────────────────────

TOOL_REG_IMPORT = "# ── AVA Tool Registry ──"
TOOL_REG_IMPORT_NEW = """# ── AVA Tool Registry ──
# Day 8: native tool support (Python handler, not shell command)
import importlib
"""

TOOL_REG_EXECUTE_ANCHOR = "    def execute(self, tool_name: str, args: dict) -> dict:"
TOOL_REG_EXECUTE_NEW = """    def register_native(self, name: str, handler, description: str,
                        args: dict, risk_level: str = "low",
                        requires_approval: bool = False,
                        available: bool = True):
        \"\"\"Register a Python callable as a tool (not a shell command).\"\"\"
        self._tools[name] = {
            "name":               name,
            "description":        description,
            "args":               args,
            "risk_level":         risk_level,
            "type":               "native",
            "handler":            handler,
            "requires_approval":  requires_approval,
            "available":          available,
        }

    def execute(self, tool_name: str, args: dict) -> dict:"""

TOOL_REG_DISPATCH_ANCHOR = "        tool = self._tools.get(tool_name)"
TOOL_REG_DISPATCH_NEW = """        tool = self._tools.get(tool_name)
        # Native tool dispatch (Python handler — not shell)
        if tool and tool.get("type") == "native":
            handler = tool.get("handler")
            if not callable(handler):
                return {"error": f"Tool '{tool_name}' has no callable handler"}
            if not tool.get("available", True):
                return {"error": f"Tool '{tool_name}' is unavailable (binary not installed)"}
            try:
                result = handler(**args) if args else handler()
                return result
            except TypeError as e:
                return {"error": f"Tool '{tool_name}' argument error: {e}"}
            except Exception as e:
                return {"error": f"Tool '{tool_name}' execution error: {e}"}
"""


def step2_patch_tool_registry():
    print("\n── Step 2: Patch tool_registry.py ───────────────────────────────")
    if not TOOL_REG.exists():
        print(f"  {ERR} tool_registry.py not found at {TOOL_REG}")
        sys.exit(1)
    backup(TOOL_REG)

    ok1 = patch_file(TOOL_REG, TOOL_REG_IMPORT, TOOL_REG_IMPORT_NEW,
                     "Add importlib import")
    ok2 = patch_file(TOOL_REG, TOOL_REG_EXECUTE_ANCHOR, TOOL_REG_EXECUTE_NEW,
                     "Add register_native() method")
    ok3 = patch_file(TOOL_REG, TOOL_REG_DISPATCH_ANCHOR, TOOL_REG_DISPATCH_NEW,
                     "Add native tool dispatch in execute()")

    if not all([ok1, ok2, ok3]):
        print(f"\n  {ERR} tool_registry.py patch incomplete.")
        print("      Apply the 3 blocks manually — see PHASE4_DAY8_COMPLETED.md")
        return False
    return True


# ─── Step 3: Patch web_agent_v2.1_guardrail.py ──────────────────────────────

# 3a — Import vuln_scanner
APP_IMPORT_ANCHOR = "from control.incident_reporter import"
APP_IMPORT_NEW = """from control.incident_reporter import
from control import vuln_scanner  # Day 8 — Trivy + Lynis
"""
# Note: real anchor will be the full import line — adjust below
APP_IMPORT_ANCHOR_REAL = "from control.incident_reporter import report_tool_execution, report_approved_execution, report_graph_execution"
APP_IMPORT_NEW_REAL    = """from control.incident_reporter import report_tool_execution, report_approved_execution, report_graph_execution
from control import vuln_scanner          # Day 8 — Trivy + Lynis scanner
"""

# 3b — Register scan tools at startup (after tool_registry is initialized)
APP_STARTUP_ANCHOR = "# ── Knowledge Base ──"
APP_STARTUP_NEW    = """# ── Vulnerability Scanner Tools (Day 8) ──────────────────────────
_vuln_tools_available = vuln_scanner.check_tools()
for _vtool in vuln_scanner.get_tool_descriptions():
    tool_registry.register_native(
        name=_vtool["name"],
        handler=_vtool["handler"],
        description=_vtool["description"],
        args=_vtool["args"],
        risk_level=_vtool["risk_level"],
        requires_approval=_vtool["requires_approval"],
        available=_vtool["available"],
    )
logger.info(
    f"[VulnScanner] Trivy={_vuln_tools_available['trivy']} "
    f"Lynis={_vuln_tools_available['lynis']}"
)

# ── Knowledge Base ──"""

# 3c — Add /scan/trivy route (convenience, not replacing /tools/<n>/run)
APP_ROUTES_ANCHOR = "# ═══════════════════════════ REPORTS ROUTES ═══════════════════════"
APP_ROUTES_NEW    = """# ═══════════════════════════ SCAN ROUTES (Day 8) ════════════════════

@app.route("/scan/trivy", methods=["POST"])
@jwt_required()
@limiter.limit("5 per minute")
def route_scan_trivy():
    \"\"\"
    POST /scan/trivy  {image: "nginx:latest"}
    Scans a Docker image for CVEs. Authenticated, rate-limited.
    Admin only (execution risk is low, but scanning may reveal sensitive info).
    \"\"\"
    from flask_jwt_extended import get_jwt_identity, get_jwt
    claims  = get_jwt()
    role    = claims.get("role", "readonly")
    if role != "admin":
        return jsonify({"error": "Admin role required"}), 403

    body  = request.get_json(silent=True) or {}
    image = body.get("image", "").strip()
    if not image:
        return jsonify({"error": "Missing 'image' field"}), 400

    user = get_jwt_identity()
    logger.info(f"[Scan] Trivy scan requested by {user}: {image}")

    result = vuln_scanner.scan_trivy(image)

    # Auto-generate incident report for critical/high findings
    _severity_report_if_needed(result, user, request.remote_addr)

    return jsonify(result)


@app.route("/scan/lynis", methods=["POST"])
@require_admin
@limiter.limit("2 per minute")
def route_scan_lynis():
    \"\"\"
    POST /scan/lynis  {}
    Runs Lynis system audit. Admin only. Requires sudo.
    \"\"\"
    from flask_jwt_extended import get_jwt_identity
    user = get_jwt_identity()
    logger.info(f"[Scan] Lynis audit requested by {user}")

    result = vuln_scanner.scan_lynis()

    # Auto-generate incident report
    _severity_report_if_needed(result, user, request.remote_addr)

    return jsonify(result)


@app.route("/scan/check", methods=["GET"])
@jwt_required()
def route_scan_check():
    \"\"\"GET /scan/check — Returns availability of Trivy and Lynis binaries.\"\"\"
    return jsonify({
        "tools": vuln_scanner.check_tools(),
        "install": {
            "trivy": "curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin",
            "lynis": "sudo apt install lynis",
        }
    })


def _severity_report_if_needed(result: dict, user: str, ip: str):
    \"\"\"Auto-generate incident report if scan found critical/high issues.\"\"\"
    try:
        if result.get("status") != "success":
            return
        risk = result.get("risk_level", "info")
        if risk not in ("critical", "high"):
            return
        # Reuse tool execution reporter for simplicity
        tool_name = f"vuln_scan_{result.get('tool', 'unknown')}"
        report_tool_execution(
            tool_name=tool_name,
            tool_args={"target": result.get("target", result.get("tool", ""))},
            result=result,
            triggered_by=user,
            ip_address=ip,
            duration=0,
        )
        logger.info(f"[Scan] Auto-generated incident report for {risk} findings")
    except Exception as e:
        logger.warning(f"[Scan] Failed to auto-report: {e}")


# ═══════════════════════════ REPORTS ROUTES ═══════════════════════"""


def step3_patch_main_app():
    print("\n── Step 3: Patch web_agent_v2.1_guardrail.py ────────────────────")
    if not MAIN_APP.exists():
        print(f"  {ERR} {MAIN_APP} not found")
        sys.exit(1)
    backup(MAIN_APP)

    ok1 = patch_file(MAIN_APP, APP_IMPORT_ANCHOR_REAL, APP_IMPORT_NEW_REAL,
                     "Import vuln_scanner")
    ok2 = patch_file(MAIN_APP, APP_STARTUP_ANCHOR, APP_STARTUP_NEW,
                     "Register scan tools at startup")
    ok3 = patch_file(MAIN_APP, APP_ROUTES_ANCHOR, APP_ROUTES_NEW,
                     "Add /scan/* routes")

    if not all([ok1, ok2, ok3]):
        print(f"\n  {ERR} Main app patch incomplete.")
        print("      Check anchors in PHASE4_DAY8_COMPLETED.md and apply manually.")
        return False
    return True


# ─── Step 4: Patch secure_executor.py whitelist ──────────────────────────────

# Add trivy to the read-only command whitelist
SEC_WHITELIST_ANCHOR = "# ── Command Whitelist ──"
SEC_WHITELIST_NEW    = """# ── Command Whitelist ──
# Day 8: Trivy image scan commands (read-only, never modifies system)
# Note: scan_trivy() calls subprocess directly — not through execute()
# This whitelist covers if anyone calls trivy via /execute_approved
_TRIVY_ALLOWED = {
    "trivy image --format json",
    "trivy image --format table",
    "trivy image --severity",
    "trivy fs",
    "trivy --version",
}
"""


def step4_patch_secure_executor():
    print("\n── Step 4: Patch secure_executor.py whitelist ───────────────────")
    if not SEC_EXEC.exists():
        print(f"  {INF} secure_executor.py not found — skipping whitelist patch")
        print(f"       (vuln_scanner uses direct subprocess, whitelist optional)")
        return True
    backup(SEC_EXEC)

    ok = patch_file(SEC_EXEC, SEC_WHITELIST_ANCHOR, SEC_WHITELIST_NEW,
                    "Add trivy to read-only whitelist")
    if not ok:
        print(f"  {INF} Could not find anchor — add trivy whitelist manually if needed")
    return True


# ─── Step 5: Install binaries ─────────────────────────────────────────────────

def step5_check_and_install():
    print("\n── Step 5: Check tool availability ─────────────────────────────")

    # Trivy
    if shutil.which("trivy"):
        result = run(["trivy", "--version"], check=False)
        ver = result.stdout.strip().split("\n")[0] if result.stdout else "unknown"
        print(f"  {OK}  Trivy installed: {ver}")
    else:
        print(f"  {INF} Trivy not found. Install with:")
        print("       curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin")
        print("       OR: sudo apt-get install trivy")
        print("       Skipping install (run manually in WSL2)")

    # Lynis
    if shutil.which("lynis"):
        result = run(["lynis", "--version"], check=False)
        ver = result.stdout.strip().split("\n")[0] if result.stdout else "unknown"
        print(f"  {OK}  Lynis installed: {ver}")
    else:
        print(f"  {INF} Lynis not found. Install with:")
        print("       sudo apt install lynis")
        print("       Skipping install (run manually in WSL2)")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  AVA — Phase 4 Day 8: Trivy + Lynis Integration")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    if not PROJECT_DIR.exists():
        print(f"\n{ERR} Project directory not found: {PROJECT_DIR}")
        print("   Update PROJECT_DIR at the top of this script.")
        sys.exit(1)

    os.chdir(PROJECT_DIR)

    results = []
    step1_copy_scanner()
    results.append(step2_patch_tool_registry())
    results.append(step3_patch_main_app())
    step4_patch_secure_executor()
    step5_check_and_install()

    print("\n" + "=" * 60)
    if all(r is not False for r in results):
        print(f"  {OK}  Day 8 patch complete.")
        print()
        print("  Next steps:")
        print("  1. Install Trivy + Lynis if not yet done (see Step 5 above)")
        print("  2. Restart AVA:")
        print("     fuser -k 5002/tcp && sleep 1 && python3 web_agent_v2.1_guardrail.py")
        print("  3. Verify startup log:")
        print("     [VulnScanner] Trivy=True Lynis=True")
        print("  4. Run: python3 test_day8.py")
    else:
        print(f"  {ERR} Some patches failed. See above. Apply manually.")
    print("=" * 60)


if __name__ == "__main__":
    main()
