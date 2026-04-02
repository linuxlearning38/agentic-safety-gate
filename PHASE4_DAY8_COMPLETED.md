# AVA — Phase 4 Day 8: Trivy + Lynis Integration

**Date:** April 2, 2026  
**Engineer:** Manoj — Senior DevOps Engineer, Delhi  
**Repo:** https://github.com/linuxlearning38/agentic-safety-gate (private, master branch)  
**Commit:** `pending`

---

## What Was Built

Vulnerability scanning as first-class AVA tools. Trivy scans Docker images for CVEs. Lynis audits the host system for security misconfigurations. Both return structured JSON (never raw strings), register as AVA tools, and auto-generate incident reports for critical/high findings.

---

## Files Created / Modified

```
/mnt/i/ai-lab/projects/devops-agent/
├── control/
│   └── vuln_scanner.py           ← NEW — Trivy + Lynis scanner module
├── patch_day8.py                 ← NEW — patcher script
└── test_day8.py                  ← NEW — test suite
```

**Modified:**
- `control/tool_registry.py` — `register_native()` + native dispatch in `execute()`
- `web_agent_v2.1_guardrail.py` — import, startup registration, /scan/* routes
- `control/secure_executor.py` — trivy added to read-only whitelist

---

## Architecture Decision: Native Tools

Day 8 introduces a new tool type: **native** (Python callable) vs existing **shell** (secure_executor).

```
Before (Days 1-7):                After (Day 8):
┌──────────────────┐              ┌──────────────────┐
│  tool_registry   │              │  tool_registry   │
│  shell tools     │              │  shell tools     │  ← unchanged
│  ↓               │              │  native tools    │  ← NEW
│  secure_executor │              │  ↓               │
│  ↓               │              │  handler()       │  ← direct call
│  subprocess      │              │  ↓               │
│  (shell command) │              │  subprocess      │
└──────────────────┘              │  (controlled)    │
                                  └──────────────────┘
```

**Why bypass secure_executor for scan tools?**  
- Trivy and Lynis are audit-only — they never modify the system  
- secure_executor's whitelist is designed for operational commands  
- Scan tools need structured JSON return values, not shell output strings  
- Lynis needs `sudo` — not appropriate for the general command whitelist

---

## New Routes

| Route | Method | Auth | Rate Limit | Description |
|---|---|---|---|---|
| `/scan/check` | GET | Any JWT | 30/min | Trivy + Lynis binary availability |
| `/scan/trivy` | POST | Admin | 5/min | Scan Docker image for CVEs |
| `/scan/lynis` | POST | Admin | 2/min | Run Lynis system audit |

### `/scan/trivy` Request
```json
{
    "image": "nginx:latest"
}
```

### `/scan/trivy` Response
```json
{
    "status": "success",
    "tool": "trivy",
    "scan_type": "image",
    "target": "nginx:latest",
    "scanned_at": "2026-04-02T09:00:00+00:00",
    "risk_level": "medium",
    "summary": {
        "CRITICAL": 0,
        "HIGH": 3,
        "MEDIUM": 12,
        "LOW": 8,
        "UNKNOWN": 0
    },
    "total_vulns": 23,
    "top_findings": [
        {
            "cve_id": "CVE-2024-...",
            "severity": "HIGH",
            "package": "openssl",
            "installed_version": "3.0.2",
            "fixed_version": "3.0.13",
            "title": "Memory corruption in X.509 parsing",
            "target": "nginx:latest (debian 12.5)"
        }
    ],
    "recommendation": "3 high-severity CVE(s) found. Review fixed_version fields and update packages."
}
```

### `/scan/lynis` Response
```json
{
    "status": "success",
    "tool": "lynis",
    "scanned_at": "2026-04-02T09:05:00+00:00",
    "hardening_index": 68,
    "risk_level": "medium",
    "warnings_count": 3,
    "suggestions_count": 42,
    "warnings": [
        "FIRE-4513|No firewall tool found|...",
        "AUTH-9286|No password aging configured|..."
    ],
    "suggestions": [
        "Consider hardening SSH configuration",
        "Install a file integrity tool"
    ],
    "tests_performed": 247,
    "lynis_version": "3.0.9",
    "recommendation": "Moderate: index 68/100. Address warnings and apply key suggestions to reach >75."
}
```

---

## Tool Registry

Both tools registered at startup:

```
[VulnScanner] Trivy=True Lynis=True
```

They appear in `/tools` and are callable by the ReAct loop:

```
scan_image_trivy   — low risk, no approval needed
scan_system_lynis  — medium risk, requires human approval
```

**ReAct invocation example:**  
User: *"Scan the python:3.9-slim image for critical CVEs"*  
AVA calls `scan_image_trivy(image="python:3.9-slim")` → parses result → answers with findings.

---

## Incident Report Auto-Generation

Scans with `risk_level = critical` or `high` auto-generate an incident report:

```
[Scan] Auto-generated incident report for critical findings
[Reporter] Saved: type=tool status=success user=admin file=09-00-01_tool_abc123.json
```

View via `/reports` API — no extra work needed.

---

## Install Instructions (WSL2 Ubuntu)

### Trivy
```bash
# Option A: Official install script (recommended)
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh \
    | sh -s -- -b /usr/local/bin

# Option B: apt
sudo apt-get install wget apt-transport-https gnupg lsb-release
wget -qO - https://aquasecurity.github.io/trivy-repo/deb/public.key | sudo apt-key add -
echo "deb https://aquasecurity.github.io/trivy-repo/deb $(lsb_release -sc) main" \
    | sudo tee -a /etc/apt/sources.list.d/trivy.list
sudo apt-get update && sudo apt-get install trivy

# Verify
trivy --version
```

### Lynis
```bash
sudo apt install lynis

# Verify
lynis --version

# Test run (no sudo — limited checks)
lynis audit system --quick --quiet

# Full run (needs sudo)
sudo lynis audit system --quick
```

---

## vuln_scanner.py Design Notes

### Error handling
All functions return `{"status": "error", "error_code": "...", "message": "..."}` — never raise exceptions. AVA can pass this directly to the LLM.

### Trivy JSON parsing
Handles multi-result output (image can have multiple layers/targets). All vulns from all targets are merged and sorted by severity.

### Lynis parsing strategy
1. **Primary:** Parse `/var/log/lynis-report.dat` (key=value, structured, reliable)  
2. **Fallback:** Parse stdout (regex-based, for environments without root-written log)

### Severity mapping
```
CRITICAL → risk_level: critical
HIGH (>5) → risk_level: high  
HIGH (>0) → risk_level: medium
MEDIUM    → risk_level: low
None      → risk_level: info
```

### tool_registry.py changes
```python
# New method:
registry.register_native(
    name="scan_image_trivy",
    handler=vuln_scanner.scan_trivy,
    description="...",
    args={"image": "Docker image name/tag"},
    risk_level="low",
    requires_approval=False,
    available=True,
)

# execute() now dispatches natively:
if tool.get("type") == "native":
    result = handler(**args)  # ← direct Python call
    return result
```

---

## Manual Patch Guide

If `patch_day8.py` anchors don't match your file, apply these manually:

### 1. tool_registry.py — add `register_native()` method

Add before your existing `execute()` method:

```python
def register_native(self, name, handler, description, args,
                    risk_level="low", requires_approval=False, available=True):
    self._tools[name] = {
        "name": name, "description": description, "args": args,
        "risk_level": risk_level, "type": "native",
        "handler": handler, "requires_approval": requires_approval,
        "available": available,
    }
```

### 2. tool_registry.py — native dispatch in `execute()`

At the top of your `execute()` method, before the existing dispatch:

```python
tool = self._tools.get(tool_name)
if tool and tool.get("type") == "native":
    handler = tool.get("handler")
    if not callable(handler):
        return {"error": f"Tool '{tool_name}' has no callable handler"}
    if not tool.get("available", True):
        return {"error": f"Tool '{tool_name}' unavailable (binary not installed)"}
    try:
        result = handler(**args) if args else handler()
        return result
    except Exception as e:
        return {"error": f"Tool '{tool_name}' error: {e}"}
```

### 3. web_agent_v2.1_guardrail.py — imports

```python
from control import vuln_scanner          # Day 8
```

### 4. web_agent_v2.1_guardrail.py — startup registration

After tool_registry is initialized, add:

```python
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
_t = vuln_scanner.check_tools()
logger.info(f"[VulnScanner] Trivy={_t['trivy']} Lynis={_t['lynis']}")
```

### 5. web_agent_v2.1_guardrail.py — routes

Add before or after your reports routes block — see full route code in `patch_day8.py`.

---

## Test Run

```bash
# Get token
TOKEN=$(curl -s -X POST http://localhost:5002/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"ava-admin-2026"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Quick tests
python3 test_day8.py --token $TOKEN

# Full tests including ReAct + auto-report
python3 test_day8.py --token $TOKEN --full

# Manual quick scan
curl -s -X POST http://localhost:5002/scan/trivy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"image":"alpine:latest"}' | python3 -m json.tool

# Check tool availability
curl -s http://localhost:5002/scan/check \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

---

## Known Issues & Notes

| Issue | Priority | Notes |
|---|---|---|
| Lynis needs sudo | Expected | WSL2 passwordless sudo works — test with `sudo lynis --version` |
| Trivy DB download on first run | Expected | Trivy pulls its vuln DB on first scan (~200MB) — takes 1-2 min |
| Lynis slow on WSL2 | Low | WSL2 /proc limitations — some checks skip silently |
| ReAct in /ask doesn't chain Lynis | Low | Lynis needs approval — ReAct pauses for human confirm |

---

## Remaining Phase 4 Work

| Day | Task | Status |
|---|---|---|
| Day 8 | Trivy + Lynis Integration | ✅ |
| Day 9 | Gunicorn + HTTPS | ⬜ |
| Day 10 | Docker Containerization | ⬜ |

---

## Git Commit

```bash
cd /mnt/i/ai-lab/projects/devops-agent/
git add control/vuln_scanner.py patch_day8.py test_day8.py \
        control/tool_registry.py web_agent_v2.1_guardrail.py \
        control/secure_executor.py PHASE4_DAY8_COMPLETED.md
git commit -m "Day 8: Trivy + Lynis vulnerability scanning integration

- control/vuln_scanner.py: Trivy CVE scanner + Lynis system auditor
- tool_registry: register_native() for Python handler tools
- /scan/trivy, /scan/lynis, /scan/check routes
- Auto-incident-report for critical/high findings
- Both tools available to ReAct loop"
```

---

*AVA — Built by Manoj | Powered by Qwen 2.5 14B + ChromaDB + Ollama*  
*Phase 4 Day 8 Complete | April 2, 2026*
