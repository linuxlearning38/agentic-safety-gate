"""
control/vuln_scanner.py
AVA — Vulnerability Scanner Module
Day 8: Trivy (container CVE) + Lynis (system audit)

Design decisions:
- Direct subprocess, NOT through secure_executor (audit-only, read-only tools)
- Returns structured dicts — no raw strings to LLM
- Auto-generates incident reports via incident_reporter
- Severity-based escalation: CRITICAL findings → high-priority report
"""

import subprocess
import json
import shutil
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
TOP_FINDINGS_LIMIT = 10   # max CVEs to include in summary (full list in report file)
LYNIS_REPORT_PATH = Path("/tmp/lynis-report.dat")
LYNIS_LOG_PATH    = Path("/var/log/lynis.log")
SCAN_TIMEOUT      = 300   # 5 min max for any scan

# ─── Tool Availability Check ─────────────────────────────────────────────────

def check_tools() -> dict:
    """Return availability of trivy and lynis binaries."""
    return {
        "trivy": shutil.which("trivy") is not None,
        "lynis": shutil.which("lynis") is not None,
    }


# ─── Trivy ───────────────────────────────────────────────────────────────────

def scan_trivy(image: str, severity_filter: Optional[str] = None) -> dict:
    """
    Scan a Docker image (or fs path) with Trivy.

    Args:
        image:           Docker image name/tag or filesystem path.
                         Examples: "nginx:latest", "python:3.11-slim", "."
        severity_filter: Comma-separated severities to include.
                         Default: all. Example: "CRITICAL,HIGH"

    Returns structured dict — never raw subprocess output.
    """
    if not shutil.which("trivy"):
        return _error_result("trivy_not_installed", "Trivy binary not found. Install: curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin")

    image = image.strip()
    if not image:
        return _error_result("invalid_input", "Image name cannot be empty")

    # Detect if scanning fs path vs docker image
    is_fs = image.startswith("/") or image in (".", "./")
    scan_type = "filesystem" if is_fs else "image"

    cmd = [
        "trivy", scan_type,
        "--format", "json",
        "--quiet",
        "--timeout", "5m",
    ]
    if severity_filter:
        cmd += ["--severity", severity_filter.upper()]
    cmd.append(image)

    logger.info(f"[Trivy] Scanning {scan_type}: {image}")
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _error_result("timeout", f"Trivy scan timed out after {SCAN_TIMEOUT}s")
    except FileNotFoundError:
        return _error_result("trivy_not_installed", "Trivy binary not found")
    except Exception as e:
        return _error_result("subprocess_error", str(e))

    if proc.returncode not in (0, 1):   # 0=no vulns, 1=vulns found
        return _error_result(
            "scan_failed",
            f"Trivy exited {proc.returncode}: {proc.stderr[:300]}",
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _error_result("parse_failed", f"Could not parse Trivy JSON output: {proc.stdout[:200]}")

    return _parse_trivy_json(data, image, scan_type, started_at)


def _parse_trivy_json(data: dict, image: str, scan_type: str, started_at: str) -> dict:
    """Extract structured findings from Trivy JSON output."""
    summary = {s: 0 for s in SEVERITY_ORDER}
    all_vulns = []

    results = data.get("Results") or []
    for result in results:
        vulns = result.get("Vulnerabilities") or []
        for v in vulns:
            sev = v.get("Severity", "UNKNOWN").upper()
            summary[sev] = summary.get(sev, 0) + 1
            all_vulns.append({
                "cve_id":            v.get("VulnerabilityID", ""),
                "severity":          sev,
                "package":           v.get("PkgName", ""),
                "installed_version": v.get("InstalledVersion", ""),
                "fixed_version":     v.get("FixedVersion", "") or "no fix available",
                "title":             v.get("Title", ""),
                "target":            result.get("Target", ""),
            })

    # Sort: CRITICAL first, then HIGH, etc.
    sev_rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    all_vulns.sort(key=lambda x: sev_rank.get(x["severity"], 99))

    total = sum(summary.values())
    risk_level = _trivy_risk_level(summary)

    return {
        "status":      "success",
        "tool":        "trivy",
        "scan_type":   scan_type,
        "target":      image,
        "scanned_at":  started_at,
        "risk_level":  risk_level,
        "summary":     summary,
        "total_vulns": total,
        "top_findings": all_vulns[:TOP_FINDINGS_LIMIT],
        "all_findings": all_vulns,          # full list — for report file
        "raw_results":  len(results),        # number of scan targets
        "recommendation": _trivy_recommendation(summary, image),
    }


def _trivy_risk_level(summary: dict) -> str:
    if summary.get("CRITICAL", 0) > 0:
        return "critical"
    if summary.get("HIGH", 0) > 5:
        return "high"
    if summary.get("HIGH", 0) > 0:
        return "medium"
    if summary.get("MEDIUM", 0) > 0:
        return "low"
    return "info"


def _trivy_recommendation(summary: dict, image: str) -> str:
    crits = summary.get("CRITICAL", 0)
    highs = summary.get("HIGH", 0)
    if crits > 0:
        return f"URGENT: {crits} critical CVE(s) in {image}. Update base image immediately and pin to a patched version."
    if highs > 0:
        return f"{highs} high-severity CVE(s) found. Review fixed_version fields and update packages."
    if sum(summary.values()) == 0:
        return f"No vulnerabilities found in {image}. Image is clean at time of scan."
    return "Low/medium findings only. Schedule routine updates."


# ─── Lynis ───────────────────────────────────────────────────────────────────

def scan_lynis(use_sudo: bool = True) -> dict:
    """
    Run a Lynis system security audit.

    Args:
        use_sudo: Run with sudo for full system access (default True).
                  Without sudo, many checks are skipped.

    Returns structured dict with hardening index, warnings, and suggestions.
    """
    if not shutil.which("lynis"):
        return _error_result("lynis_not_installed", "Lynis binary not found. Install: sudo apt install lynis")

    cmd = []
    if use_sudo:
        cmd = ["sudo", "lynis", "audit", "system",
               "--quick", "--quiet", "--no-colors", "--cronjob", "--report-file", "/tmp/lynis-report.dat"]
    else:
        cmd = ["lynis", "audit", "system",
               "--quick", "--quiet", "--no-colors"]

    logger.info(f"[Lynis] Starting system audit (sudo={use_sudo})")
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _error_result("timeout", f"Lynis audit timed out after {SCAN_TIMEOUT}s")
    except FileNotFoundError:
        return _error_result("lynis_not_installed", "Lynis or sudo binary not found")
    except Exception as e:
        return _error_result("subprocess_error", str(e))

    # Lynis exit codes: 0=ok, 1=error, 64=warnings found — all valid for us
    if proc.returncode not in (0, 1, 64):
        return _error_result(
            "scan_failed",
            f"Lynis exited {proc.returncode}: {proc.stderr[:300]}",
        )

    # Prefer parsing the structured report file over stdout
    if LYNIS_REPORT_PATH.exists():
        try:
            return _parse_lynis_report_dat(started_at)
        except Exception as e:
            logger.warning(f"[Lynis] Failed to parse report.dat: {e} — falling back to stdout")

    return _parse_lynis_stdout(proc.stdout, started_at)


def _parse_lynis_report_dat(started_at: str) -> dict:
    """
    Parse /var/log/lynis-report.dat (key=value format).
    This is the most reliable source — structured, machine-readable.
    """
    dat = {}
    with open(LYNIS_REPORT_PATH, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) > 500: continue  # skip bulk fields (installed_packages etc)
            if key in dat:
                # Multi-value keys → convert to list
                existing = dat[key]
                if isinstance(existing, list):
                    existing.append(val)
                else:
                    dat[key] = [existing, val]
            else:
                dat[key] = val

    def _list(key):
        v = dat.get(key, [])
        return v if isinstance(v, list) else ([v] if v else [])

    hardening_index = int(dat.get("hardening_index", 0))
    warnings        = _list("warning")
    suggestions     = _list("suggestion")
    risk_level      = _lynis_risk_level(hardening_index, len(warnings))

    return {
        "status":           "success",
        "tool":             "lynis",
        "scanned_at":       started_at,
        "hardening_index":  hardening_index,
        "risk_level":       risk_level,
        "warnings_count":   len(warnings),
        "suggestions_count": len(suggestions),
        "warnings":         warnings[:20],          # top 20
        "suggestions":      suggestions[:20],       # top 20
        "os":               dat.get("os", "unknown"),
        "os_version":       dat.get("os_version", "unknown"),
        "kernel_version":   dat.get("kernel_version", "unknown"),
        "tests_performed":  int(dat.get("tests_executed", 0)),
        "lynis_version":    dat.get("lynis_version", "unknown"),
        "recommendation":   _lynis_recommendation(hardening_index, len(warnings)),
        "report_file":      str(LYNIS_REPORT_PATH),
    }


def _parse_lynis_stdout(stdout: str, started_at: str) -> dict:
    """
    Fallback: parse Lynis stdout for hardening index and key metrics.
    Less reliable than report.dat but always available.
    """
    lines = stdout.splitlines()

    hardening_index = 0
    warnings = []
    suggestions = []
    in_warnings = False
    in_suggestions = False

    for line in lines:
        # Hardening index
        m = re.search(r"Hardening index\s*[:\|]\s*(\d+)", line, re.IGNORECASE)
        if m:
            hardening_index = int(m.group(1))

        # Section markers
        if "Warnings:" in line or "== Warnings ==" in line:
            in_warnings = True
            in_suggestions = False
            continue
        if "Suggestions:" in line or "== Suggestions ==" in line:
            in_suggestions = True
            in_warnings = False
            continue

        # Warning lines start with "!"
        if in_warnings and line.strip().startswith("!"):
            w = line.strip().lstrip("!").strip()
            if w:
                warnings.append(w)

        # Suggestion lines start with "-"
        if in_suggestions and line.strip().startswith("-"):
            s = line.strip().lstrip("-").strip()
            if s:
                suggestions.append(s)

    risk_level = _lynis_risk_level(hardening_index, len(warnings))

    return {
        "status":           "success",
        "tool":             "lynis",
        "scanned_at":       started_at,
        "hardening_index":  hardening_index,
        "risk_level":       risk_level,
        "warnings_count":   len(warnings),
        "suggestions_count": len(suggestions),
        "warnings":         warnings[:20],
        "suggestions":      suggestions[:20],
        "tests_performed":  0,   # not available from stdout
        "recommendation":   _lynis_recommendation(hardening_index, len(warnings)),
        "note":             "Parsed from stdout (report.dat not available)",
    }


def _lynis_risk_level(hardening_index: int, warning_count: int) -> str:
    if hardening_index < 40 or warning_count > 10:
        return "critical"
    if hardening_index < 60 or warning_count > 5:
        return "high"
    if hardening_index < 75 or warning_count > 0:
        return "medium"
    return "low"


def _lynis_recommendation(hardening_index: int, warning_count: int) -> str:
    if hardening_index < 40:
        return f"Critical: hardening index {hardening_index}/100. System is significantly under-hardened. Address all warnings immediately."
    if hardening_index < 60:
        return f"High risk: index {hardening_index}/100, {warning_count} warning(s). Review and resolve warnings before production deployment."
    if hardening_index < 75:
        return f"Moderate: index {hardening_index}/100. Address warnings and apply key suggestions to reach >75."
    return f"Good hardening posture: index {hardening_index}/100. Review remaining suggestions for further improvement."


# ─── Shared Utilities ─────────────────────────────────────────────────────────

def _error_result(error_code: str, message: str) -> dict:
    logger.warning(f"[VulnScanner] {error_code}: {message}")
    return {
        "status":     "error",
        "error_code": error_code,
        "message":    message,
    }


def get_tool_descriptions() -> list:
    """
    Return tool descriptors for registration in tool_registry.
    Called by patch_day8.py during startup patching.
    """
    tools_available = check_tools()
    return [
        {
            "name":        "scan_image_trivy",
            "description": (
                "Scan a Docker image or local filesystem path for CVEs using Trivy. "
                "Returns severity breakdown (CRITICAL/HIGH/MEDIUM/LOW) and top findings. "
                "Use for: 'scan nginx:latest', 'check python:3.11 for vulnerabilities', "
                "'is this image safe to deploy?'"
            ),
            "args":         {"image": "Docker image name/tag or filesystem path to scan"},
            "risk_level":   "low",
            "type":         "native",
            "handler":      scan_trivy,
            "available":    tools_available["trivy"],
            "requires_approval": False,
        },
        {
            "name":        "scan_system_lynis",
            "description": (
                "Run a Lynis system security audit on the host. "
                "Returns hardening index (0-100), warnings, and improvement suggestions. "
                "Use for: 'audit this server', 'check system security posture', "
                "'what is the hardening index?', 'run security baseline check'"
            ),
            "args":         {},
            "risk_level":   "medium",
            "type":         "native",
            "handler":      scan_lynis,
            "available":    tools_available["lynis"],
            "requires_approval": True,    # requires sudo — human confirms
        },
    ]
