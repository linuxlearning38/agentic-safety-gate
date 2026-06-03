"""
AVA Intent Router — patch for cve_scanner
Add these entries to your existing intent_router.py / INTENT_PATTERNS dict.

Also contains the full pytest test suite for cve_scanner.
"""

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: Intent Router Patch
# Add to your INTENT_PATTERNS dict in intent_router.py
# ─────────────────────────────────────────────────────────────────────────────

CVE_SCANNER_INTENT_PATTERNS = {
    "cve_scan": {
        "tool": "cve_scanner",
        "keywords": [
            "cve", "vulnerability", "vulnerabilities", "vuln",
            "security scan", "patch scan", "ubuntu cve",
            "affected packages", "security advisory", "usn",
            "cvss", "exploit", "security audit", "check packages",
            "what packages are vulnerable", "scan for cve",
        ],
        "examples": [
            "scan for CVEs on this server",
            "what vulnerabilities does this Ubuntu host have",
            "check openssl CVEs",
            "show me HIGH severity CVEs",
            "are there any critical vulnerabilities",
            "run a security scan",
            "check CVE for curl and bash",
            "what USNs affect my system",
        ],
        "default_params": {
            "host": "localhost",
            "severity_filter": "HIGH",
            "package_filter": None,
        }
    }
}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: Test Suite
# Run with: pytest tests/test_cve_scanner.py -v
# ─────────────────────────────────────────────────────────────────────────────

import pytest
import json
from unittest.mock import patch, MagicMock
from dataclasses import asdict

# Adjust import path to your AVA structure
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from cve_scanner import (
    run_cve_scan, execute, get_ubuntu_release,
    get_patchable_packages, query_ubuntu_cves,
    CVEFinding, ScanResult, TOOL_DEFINITION
)


# ─────────────────────
# Fixtures
# ─────────────────────

MOCK_PACKAGES = {
    "curl": "7.81.0-1ubuntu1.15",
    "openssl": "3.0.2-0ubuntu1.14",
    "bash": "5.1-6ubuntu1",
    "openssh-server": "1:8.9p1-3ubuntu0.6",
    "python3": "3.10.6-1~22.04",
}

MOCK_CVE_RESPONSE = [
    {
        "id": "CVE-2024-12345",
        "description": "A critical buffer overflow in curl allows remote code execution.",
        "published": "2024-01-15T00:00:00",
        "cvss": [{"score": 9.8}],
        "statuses": [
            {"release_codename": "jammy", "status": "needed", "fixed_version": "7.81.0-1ubuntu1.16"}
        ],
        "notices": [{"id": "USN-6000-1"}]
    },
    {
        "id": "CVE-2023-99999",
        "description": "Medium severity info disclosure in curl.",
        "published": "2023-11-01T00:00:00",
        "cvss": [{"score": 5.3}],
        "statuses": [
            {"release_codename": "jammy", "status": "not-affected", "fixed_version": ""}
        ],
        "notices": []
    }
]

MOCK_APT_OUTPUT = """
Reading package lists...
Building dependency tree...
Inst curl [7.81.0-1ubuntu1.15] (7.81.0-1ubuntu1.16 Ubuntu:22.04/jammy-security [amd64])
Inst openssl [3.0.2-0ubuntu1.14] (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-security [amd64])
"""


# ─────────────────────
# Unit Tests
# ─────────────────────

class TestToolDefinition:
    def test_tool_name(self):
        assert TOOL_DEFINITION["name"] == "cve_scanner"

    def test_risk_tier_is_1(self):
        assert TOOL_DEFINITION["risk_tier"] == 1

    def test_no_confirmation_required(self):
        assert TOOL_DEFINITION["requires_confirmation"] is False

    def test_required_parameters_present(self):
        params = TOOL_DEFINITION["parameters"]
        assert "host" in params
        assert "severity_filter" in params
        assert "package_filter" in params


class TestGetUbuntuRelease:
    def test_reads_os_release(self, tmp_path):
        fake_os_release = tmp_path / "os-release"
        fake_os_release.write_text('VERSION_CODENAME=jammy\nNAME="Ubuntu"\n')
        with patch("builtins.open", return_value=open(fake_os_release)):
            result = get_ubuntu_release()
        assert result == "jammy"

    def test_returns_unknown_on_failure(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = get_ubuntu_release()
        assert result == "unknown"


class TestGetPatchablePackages:
    def test_parses_apt_output(self):
        mock_result = MagicMock()
        mock_result.stdout = MOCK_APT_OUTPUT
        with patch("subprocess.run", return_value=mock_result):
            patchable = get_patchable_packages()
        assert "curl" in patchable
        assert "openssl" in patchable
        assert "bash" not in patchable

    def test_returns_empty_on_failure(self):
        with patch("subprocess.run", side_effect=Exception("permission denied")):
            patchable = get_patchable_packages()
        assert patchable == set()


class TestQueryUbuntuCVEs:
    def test_returns_affected_cves_only(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_CVE_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            findings = query_ubuntu_cves("curl", "jammy")

        # CVE-2024-12345: needed → should be included
        # CVE-2023-99999: not-affected → should be excluded
        assert len(findings) == 1
        assert findings[0].cve_id == "CVE-2024-12345"
        assert findings[0].severity == "CRITICAL"
        assert findings[0].cvss_score == 9.8
        assert findings[0].usn_id == "USN-6000-1"
        assert findings[0].fixed_version == "7.81.0-1ubuntu1.16"

    def test_handles_api_failure_gracefully(self):
        with patch("requests.get", side_effect=Exception("timeout")):
            findings = query_ubuntu_cves("curl", "jammy")
        assert findings == []

    def test_handles_non_200_response(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("requests.get", return_value=mock_resp):
            findings = query_ubuntu_cves("nonexistent-pkg", "jammy")
        assert findings == []


class TestRunCVEScan:
    def _mock_scan_setup(self, mock_run, mock_get, mock_open_release):
        # dpkg-query mock
        dpkg_result = MagicMock()
        dpkg_result.stdout = "curl\t7.81.0-1ubuntu1.15\nopenssl\t3.0.2-0ubuntu1.14\n"

        # apt simulate mock
        apt_result = MagicMock()
        apt_result.stdout = MOCK_APT_OUTPUT

        mock_run.side_effect = [dpkg_result, apt_result]

        # Ubuntu Security API mock
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.json.return_value = MOCK_CVE_RESPONSE
        mock_get.return_value = api_resp

        # os-release mock
        mock_open_release.return_value.__enter__ = lambda s: s
        mock_open_release.return_value.__exit__ = MagicMock(return_value=False)
        mock_open_release.return_value.__iter__ = lambda s: iter(["VERSION_CODENAME=jammy\n"])

    @patch("builtins.open")
    @patch("requests.get")
    @patch("subprocess.run")
    def test_full_scan_returns_scan_result(self, mock_run, mock_get, mock_open):
        self._mock_scan_setup(mock_run, mock_get, mock_open)
        result = run_cve_scan(severity_filter="LOW")
        assert isinstance(result, ScanResult)
        assert result.host == "localhost"
        assert result.vulnerabilities_found >= 0

    @patch("builtins.open")
    @patch("requests.get")
    @patch("subprocess.run")
    def test_severity_filter_high_excludes_medium(self, mock_run, mock_get, mock_open):
        self._mock_scan_setup(mock_run, mock_get, mock_open)
        result = run_cve_scan(severity_filter="HIGH")
        for f in result.findings:
            assert f["severity"] in ("HIGH", "CRITICAL")

    @patch("builtins.open")
    @patch("requests.get")
    @patch("subprocess.run")
    def test_package_filter_limits_scan(self, mock_run, mock_get, mock_open):
        """Only specified packages should be queried"""
        self._mock_scan_setup(mock_run, mock_get, mock_open)
        result = run_cve_scan(package_filter=["curl"])
        # Only curl was requested
        for f in result.findings:
            assert f["package"] == "curl"

    @patch("builtins.open")
    @patch("requests.get")
    @patch("subprocess.run")
    def test_patchable_now_flag_set(self, mock_run, mock_get, mock_open):
        self._mock_scan_setup(mock_run, mock_get, mock_open)
        result = run_cve_scan(severity_filter="LOW")
        curl_findings = [f for f in result.findings if f["package"] == "curl"]
        if curl_findings:
            assert curl_findings[0]["patchable_now"] is True

    def test_remote_host_raises(self):
        result = run_cve_scan(host="192.168.1.100")
        assert len(result.errors) > 0
        assert "localhost" in result.errors[0] or "Remote" in result.errors[0]

    def test_findings_sorted_by_severity(self):
        findings = [
            CVEFinding("CVE-2024-001", "pkg1", "1.0", "1.1", "LOW", 3.1, "", None, "", "ubuntu"),
            CVEFinding("CVE-2024-002", "pkg2", "1.0", "1.1", "CRITICAL", 9.8, "", None, "", "ubuntu"),
            CVEFinding("CVE-2024-003", "pkg3", "1.0", "1.1", "HIGH", 7.5, "", None, "", "ubuntu"),
            CVEFinding("CVE-2024-004", "pkg4", "1.0", "1.1", "MEDIUM", 5.0, "", None, "", "ubuntu"),
        ]
        from cve_scanner import SEVERITY_ORDER
        sorted_findings = sorted(
            findings,
            key=lambda x: (SEVERITY_ORDER.get(x.severity, 0), x.cvss_score),
            reverse=True
        )
        assert sorted_findings[0].severity == "CRITICAL"
        assert sorted_findings[1].severity == "HIGH"
        assert sorted_findings[2].severity == "MEDIUM"
        assert sorted_findings[3].severity == "LOW"


class TestExecuteEntryPoint:
    @patch("cve_scanner.run_cve_scan")
    def test_execute_passes_params(self, mock_scan):
        mock_scan.return_value = ScanResult(
            scan_time="2024-01-01T00:00:00+00:00",
            host="localhost", ubuntu_release="jammy",
            packages_scanned=2, vulnerabilities_found=1,
            critical=1, high=0, medium=0, low=0
        )
        result = execute({
            "host": "localhost",
            "severity_filter": "CRITICAL",
            "package_filter": ["curl"],
        })
        mock_scan.assert_called_once_with(
            host="localhost",
            severity_filter="CRITICAL",
            package_filter=["curl"],
            nvd_api_key=None,
        )
        assert isinstance(result, dict)
        assert "vulnerabilities_found" in result

    @patch("cve_scanner.run_cve_scan")
    def test_execute_uses_defaults(self, mock_scan):
        mock_scan.return_value = ScanResult(
            scan_time="2024-01-01T00:00:00+00:00",
            host="localhost", ubuntu_release="jammy",
            packages_scanned=0, vulnerabilities_found=0,
            critical=0, high=0, medium=0, low=0
        )
        execute({})
        mock_scan.assert_called_once_with(
            host="localhost",
            severity_filter="HIGH",
            package_filter=None,
            nvd_api_key=None,
        )


# ─────────────────────
# Integration smoke test
# Run only if LIVE_TEST=1 env var is set
# ─────────────────────

@pytest.mark.skipif(
    os.environ.get("LIVE_TEST") != "1",
    reason="Set LIVE_TEST=1 to run against real Ubuntu Security API"
)
def test_live_scan_curl():
    """Live integration test — hits real Ubuntu Security API"""
    result = run_cve_scan(
        severity_filter="LOW",
        package_filter=["curl"]
    )
    assert result.packages_scanned == 1
    assert isinstance(result.findings, list)
    print(f"\nLive result: {result.vulnerabilities_found} CVEs for curl")
    print(json.dumps(result.findings[:2], indent=2))
