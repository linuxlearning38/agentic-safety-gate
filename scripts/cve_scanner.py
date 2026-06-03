"""
AVA Tool #14: cve_scanner
Risk Tier: 1 (READ-ONLY)
Purpose: Query Ubuntu Security API + NVD for CVEs affecting a target host's installed packages
Author: AVA DevSecOps Platform
"""

import subprocess
import requests
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, asdict, field

logger = logging.getLogger("ava.tools.cve_scanner")

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

UBUNTU_SECURITY_API = "https://ubuntu.com/security/api/v1/cves"
NVD_API             = "https://services.nvd.nist.gov/rest/json/cves/2.0"

SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}

# ─────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────

@dataclass
class CVEFinding:
    cve_id: str
    package: str
    installed_version: str
    fixed_version: str
    severity: str           # CRITICAL / HIGH / MEDIUM / LOW
    cvss_score: float
    description: str
    usn_id: Optional[str]   # Ubuntu Security Notice ID
    published: str
    source: str             # "ubuntu" | "nvd"
    patchable_now: bool = False

@dataclass
class ScanResult:
    scan_time: str
    host: str
    ubuntu_release: str
    packages_scanned: int
    vulnerabilities_found: int
    critical: int
    high: int
    medium: int
    low: int
    findings: list = field(default_factory=list)
    errors: list  = field(default_factory=list)

# ─────────────────────────────────────────────
# Package Inventory
# ─────────────────────────────────────────────

def get_installed_packages(host: str = "localhost") -> dict:
    """
    Returns {package_name: installed_version} from target host.
    v1: localhost only. Remote host support requires SSH executor (Tool B).
    """
    if host != "localhost":
        raise NotImplementedError(
            "Remote host scanning requires SSH executor (Tool B). "
            "Use host='localhost' for local scans."
        )
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Package}\t${Version}\n"],
            capture_output=True, text=True, timeout=30
        )
        packages = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                packages[parts[0].strip()] = parts[1].strip()
        logger.info(f"Loaded {len(packages)} installed packages from dpkg")
        return packages
    except Exception as e:
        logger.error(f"dpkg-query failed: {e}")
        raise RuntimeError(f"Cannot enumerate packages: {e}")


def get_ubuntu_release() -> str:
    """Get Ubuntu codename e.g. 'jammy', 'focal'"""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("VERSION_CODENAME="):
                    return line.split("=")[1].strip().strip('"')
    except Exception:
        pass
    return "unknown"

# ─────────────────────────────────────────────
# Ubuntu Security API
# ─────────────────────────────────────────────

def query_ubuntu_cves(package: str, release: str) -> list:
    """
    Query Ubuntu Security API for CVEs affecting a specific package on a given release.
    API: https://ubuntu.com/security/api/v1
    Rate limit: generous, no key required.
    """
    findings = []
    try:
        params = {"package": package, "limit": 20, "order": "descending"}
        resp = requests.get(UBUNTU_SECURITY_API, params=params, timeout=10)
        if resp.status_code != 200:
            return []

        data = resp.json()
        cves = data if isinstance(data, list) else data.get("cves", [])

        for cve in cves:
            cve_id = cve.get("id", "")
            if not cve_id.startswith("CVE-"):
                continue

            # Check if this Ubuntu release is affected
            statuses = cve.get("statuses", [])
            release_status = None
            for s in statuses:
                if s.get("release_codename", "").lower() == release.lower():
                    release_status = s
                    break

            if not release_status:
                continue

            status = release_status.get("status", "")
            if status in ("not-affected", "DNE"):
                continue

            fixed_version = release_status.get("fixed_version", "") or "No fix available"

            # CVSS → severity
            cvss_score = 0.0
            for cvss in cve.get("cvss", []):
                score = float(cvss.get("score", 0.0))
                if score > cvss_score:
                    cvss_score = score

            if   cvss_score >= 9.0: severity = "CRITICAL"
            elif cvss_score >= 7.0: severity = "HIGH"
            elif cvss_score >= 4.0: severity = "MEDIUM"
            elif cvss_score > 0:    severity = "LOW"
            else:                   severity = "UNKNOWN"

            usns   = cve.get("notices", [])
            usn_id = usns[0].get("id") if usns else None

            findings.append(CVEFinding(
                cve_id=cve_id,
                package=package,
                installed_version="",       # filled by caller
                fixed_version=fixed_version,
                severity=severity,
                cvss_score=cvss_score,
                description=cve.get("description", "No description")[:300],
                usn_id=usn_id,
                published=cve.get("published", ""),
                source="ubuntu"
            ))

    except requests.RequestException as e:
        logger.warning(f"Ubuntu Security API failed for {package}: {e}")

    return findings

# ─────────────────────────────────────────────
# NVD Enrichment (optional)
# ─────────────────────────────────────────────

def query_nvd_cve(cve_id: str, api_key: Optional[str] = None) -> Optional[dict]:
    """
    Fetch CVE details from NVD as enrichment/fallback.
    Rate limits: 5 req/30s (no key) | 50 req/30s (with key).
    Set NVD_API_KEY env var in production.
    """
    headers = {"apiKey": api_key} if api_key else {}
    try:
        resp = requests.get(
            NVD_API, params={"cveId": cve_id},
            headers=headers, timeout=10
        )
        if resp.status_code == 200:
            vulns = resp.json().get("vulnerabilities", [])
            if vulns:
                return vulns[0].get("cve", {})
    except Exception as e:
        logger.warning(f"NVD lookup failed for {cve_id}: {e}")
    return None

# ─────────────────────────────────────────────
# apt simulate — patchable check
# ─────────────────────────────────────────────

def get_patchable_packages() -> set:
    """
    Run apt-get --simulate upgrade to determine which packages
    can actually be patched right now via apt.
    READ-ONLY: simulate flag guarantees no changes.
    """
    try:
        result = subprocess.run(
            ["apt-get", "--simulate", "upgrade"],
            capture_output=True, text=True, timeout=60
        )
        patchable = set()
        for line in result.stdout.splitlines():
            # "Inst curl [7.81.0-1ubuntu1.15] (7.81.0-1ubuntu1.16 Ubuntu:22.04 [amd64])"
            if line.startswith("Inst "):
                parts = line.split()
                if len(parts) >= 2:
                    patchable.add(parts[1])
        logger.info(f"apt simulate: {len(patchable)} upgradeable packages")
        return patchable
    except Exception as e:
        logger.warning(f"apt simulate failed (non-root?): {e}")
        return set()

# ─────────────────────────────────────────────
# Main Scanner
# ─────────────────────────────────────────────

def run_cve_scan(
    host: str = "localhost",
    severity_filter: Optional[str] = "HIGH",
    package_filter: Optional[list] = None,
    nvd_api_key: Optional[str] = None,
    max_packages: int = 500
) -> ScanResult:
    """
    Main entry point. Called by AVA's ReAct loop via execute().

    Args:
        host:             Target host ('localhost' only in v1)
        severity_filter:  Minimum severity: LOW | MEDIUM | HIGH | CRITICAL
        package_filter:   Specific packages to check (None = all installed)
        nvd_api_key:      NVD API key for higher rate limits
        max_packages:     Safety cap to prevent API flooding

    Returns:
        ScanResult with full findings list sorted by severity
    """
    scan_time = datetime.now(timezone.utc).isoformat()
    errors    = []

    # 1. Enumerate installed packages
    logger.info(f"CVE scan start — host={host}")
    try:
        installed = get_installed_packages(host)
    except Exception as e:
        return ScanResult(
            scan_time=scan_time, host=host, ubuntu_release="unknown",
            packages_scanned=0, vulnerabilities_found=0,
            critical=0, high=0, medium=0, low=0,
            findings=[], errors=[str(e)]
        )

    release = get_ubuntu_release()
    logger.info(f"Ubuntu release detected: {release}")

    # 2. Apply package filter
    if package_filter:
        installed = {k: v for k, v in installed.items() if k in package_filter}

    # 3. Safety cap
    pkg_list = list(installed.items())[:max_packages]

    # 4. Get currently patchable packages (apt simulate)
    patchable = get_patchable_packages()

    # 5. Query Ubuntu Security API
    min_sev = SEVERITY_ORDER.get(severity_filter or "LOW", 0)
    all_findings = []

    for pkg_name, installed_version in pkg_list:
        findings = query_ubuntu_cves(pkg_name, release)
        for f in findings:
            f.installed_version = installed_version
            f.patchable_now     = pkg_name in patchable
            if SEVERITY_ORDER.get(f.severity, 0) >= min_sev:
                all_findings.append(f)

    # 6. Sort: severity DESC, CVSS DESC
    all_findings.sort(
        key=lambda x: (SEVERITY_ORDER.get(x.severity, 0), x.cvss_score),
        reverse=True
    )

    # 7. Deduplicate by (CVE, package)
    seen, deduped = set(), []
    for f in all_findings:
        key = (f.cve_id, f.package)
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    # 8. Severity counts
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in deduped:
        if f.severity in counts:
            counts[f.severity] += 1

    logger.info(
        f"Scan complete — {len(deduped)} CVEs "
        f"[C={counts['CRITICAL']} H={counts['HIGH']} "
        f"M={counts['MEDIUM']} L={counts['LOW']}]"
    )

    return ScanResult(
        scan_time=scan_time,
        host=host,
        ubuntu_release=release,
        packages_scanned=len(pkg_list),
        vulnerabilities_found=len(deduped),
        critical=counts["CRITICAL"],
        high=counts["HIGH"],
        medium=counts["MEDIUM"],
        low=counts["LOW"],
        findings=[asdict(f) for f in deduped],
        errors=errors
    )

# ─────────────────────────────────────────────
# AVA Tool Registry Integration
# ─────────────────────────────────────────────

TOOL_DEFINITION = {
    "name": "cve_scanner",
    "description": (
        "Scan a Ubuntu host for known CVEs affecting installed packages. "
        "Queries Ubuntu Security API and checks apt upgrade candidates. "
        "READ-ONLY — does not modify the system in any way."
    ),
    "risk_tier": 1,
    "requires_confirmation": False,
    "intent_families": ["security_audit", "vulnerability_scan"],
    "parameters": {
        "host": {
            "type": "string",
            "default": "localhost",
            "description": "Target host. Only 'localhost' supported in v1."
        },
        "severity_filter": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
            "default": "HIGH",
            "description": "Minimum severity to return."
        },
        "package_filter": {
            "type": "array",
            "items": {"type": "string"},
            "default": None,
            "description": "Specific packages to scan. Omit for full system scan."
        },
        "nvd_api_key": {
            "type": "string",
            "default": None,
            "description": "Optional NVD API key (higher rate limits)."
        }
    }
}


def execute(params: dict) -> dict:
    """
    AVA tool registry entry point.
    Called by the ReAct loop when intent router selects cve_scanner.
    """
    result = run_cve_scan(
        host=params.get("host", "localhost"),
        severity_filter=params.get("severity_filter", "HIGH"),
        package_filter=params.get("package_filter"),
        nvd_api_key=params.get("nvd_api_key"),
    )
    return asdict(result)


# ─────────────────────────────────────────────
# Quick Test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Running CVE scan — HIGH+ severity, key packages only...")
    result = run_cve_scan(
        severity_filter="HIGH",
        package_filter=["curl", "openssl", "openssh-server", "bash", "python3", "libc6"]
    )
    print(json.dumps(asdict(result), indent=2))
