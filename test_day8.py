#!/usr/bin/env python3
"""
test_day8.py
AVA Phase 4 — Day 8 Test Suite: Trivy + Lynis

Tests:
  - Binary availability check endpoint
  - Trivy scan (clean image, vuln image, bad input)
  - Lynis scan
  - Auth gating on scan routes
  - Auto-report generation on critical findings
  - Tool registration (scan tools appear in /tools)
  - ReAct integration (AVA calls scan tools autonomously)

Usage:
  TOKEN=$(curl -s -X POST http://localhost:5002/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"ava-admin-2026"}' \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
  
  python3 test_day8.py --token $TOKEN
"""

import sys
import json
import time
import argparse
import requests
from datetime import datetime

BASE    = "http://localhost:5002"
TIMEOUT = 30

PASS = "✅"
FAIL = "❌"
SKIP = "⏭️ "
INFO = "ℹ️ "


class TestRunner:
    def __init__(self, token: str):
        self.token   = token
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.results = []

    def _req(self, method, path, body=None, headers=None, expected_status=200):
        url = f"{BASE}{path}"
        h = headers or self.headers
        try:
            r = getattr(requests, method)(url, json=body, headers=h, timeout=TIMEOUT)
            return r
        except requests.exceptions.ConnectionError:
            return None

    def check(self, name: str, passed: bool, note: str = ""):
        icon = PASS if passed else FAIL
        line = f"  {icon}  {name}"
        if note:
            line += f"  [{note}]"
        print(line)
        self.results.append({"name": name, "passed": passed, "note": note})

    def skip(self, name: str, reason: str):
        print(f"  {SKIP}  {name}  [{reason}]")
        self.results.append({"name": name, "passed": None, "note": reason})

    # ── Tests ────────────────────────────────────────────────────────────────

    def test_scan_check(self):
        """GET /scan/check — tool availability"""
        r = self._req("get", "/scan/check")
        if r is None:
            self.check("GET /scan/check reachable", False, "Connection refused — is AVA running?")
            return

        self.check("GET /scan/check returns 200", r.status_code == 200)
        if r.status_code == 200:
            data = r.json()
            tools = data.get("tools", {})
            self.check("/scan/check has trivy key",  "trivy" in tools)
            self.check("/scan/check has lynis key",  "lynis" in tools)
            self.check("/scan/check has install hints", "install" in data)
            print(f"     {INFO} Trivy={tools.get('trivy')} Lynis={tools.get('lynis')}")

    def test_trivy_no_auth(self):
        """POST /scan/trivy without token → 401"""
        r = self._req("post", "/scan/trivy", body={"image": "nginx:latest"},
                      headers={"Content-Type": "application/json"})
        if r is None:
            self.skip("Trivy unauthenticated → 401", "AVA not running")
            return
        self.check("POST /scan/trivy without token → 401", r.status_code == 401)

    def test_trivy_missing_image(self):
        """POST /scan/trivy with no image → 400"""
        r = self._req("post", "/scan/trivy", body={})
        if r is None:
            self.skip("Trivy missing image → 400", "AVA not running")
            return
        self.check("POST /scan/trivy missing image → 400", r.status_code == 400)

    def test_trivy_scan_real(self, image: str = "alpine:latest"):
        """POST /scan/trivy with real image — check response structure"""
        r = self._req("post", "/scan/trivy", body={"image": image})
        if r is None:
            self.skip(f"Trivy scan {image}", "AVA not running")
            return

        if r.status_code != 200:
            self.check(f"Trivy scan {image} → 200", False, f"Got {r.status_code}: {r.text[:100]}")
            return

        data = r.json()
        self.check(f"Trivy scan {image} → 200", True)
        self.check("Response has 'status' field",    "status" in data)
        self.check("Response has 'summary' field",   "summary" in data)
        self.check("Response has 'risk_level'",      "risk_level" in data)
        self.check("Response has 'total_vulns'",     "total_vulns" in data)
        self.check("Response has 'recommendation'",  "recommendation" in data)
        self.check("Summary has severity keys",
                   all(k in data.get("summary", {}) for k in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]))

        if data.get("status") == "error" and data.get("error_code") == "trivy_not_installed":
            print(f"     {INFO} Trivy not installed — install with install.sh")
        else:
            print(f"     {INFO} Risk: {data.get('risk_level')} | Total CVEs: {data.get('total_vulns')}")
            summary = data.get("summary", {})
            print(f"     {INFO} CRITICAL={summary.get('CRITICAL',0)} HIGH={summary.get('HIGH',0)} "
                  f"MEDIUM={summary.get('MEDIUM',0)} LOW={summary.get('LOW',0)}")

    def test_lynis_no_admin(self):
        """POST /scan/lynis with readonly token → 403"""
        # Get readonly token first
        r_login = requests.post(
            f"{BASE}/auth/login",
            json={"username": "readonly", "password": "ava-readonly-2026"},
            timeout=TIMEOUT,
        )
        if r_login.status_code != 200:
            self.skip("Lynis → 403 for readonly", "Could not get readonly token")
            return
        ro_token = r_login.json().get("access_token", "")
        ro_headers = {"Authorization": f"Bearer {ro_token}", "Content-Type": "application/json"}

        r = self._req("post", "/scan/lynis", body={}, headers=ro_headers)
        if r is None:
            self.skip("Lynis → 403 for readonly", "AVA not running")
            return
        self.check("POST /scan/lynis readonly → 403", r.status_code == 403)

    def test_lynis_scan(self):
        """POST /scan/lynis — check response structure (may need sudo)"""
        r = self._req("post", "/scan/lynis", body={})
        if r is None:
            self.skip("Lynis scan", "AVA not running")
            return

        if r.status_code != 200:
            self.check("Lynis scan → 200", False, f"Got {r.status_code}")
            return

        data = r.json()
        self.check("Lynis scan → 200", True)
        self.check("Response has 'status'",          "status" in data)
        self.check("Response has 'hardening_index'", "hardening_index" in data)
        self.check("Response has 'risk_level'",      "risk_level" in data)
        self.check("Response has 'warnings'",        "warnings" in data)
        self.check("Response has 'recommendations'", "recommendation" in data)

        if data.get("status") == "error" and data.get("error_code") == "lynis_not_installed":
            print(f"     {INFO} Lynis not installed — run: sudo apt install lynis")
        else:
            print(f"     {INFO} Hardening index: {data.get('hardening_index')}/100")
            print(f"     {INFO} Risk: {data.get('risk_level')} | Warnings: {data.get('warnings_count')}")

    def test_scan_tools_in_registry(self):
        """GET /tools — scan tools appear in tool list"""
        r = self._req("get", "/tools")
        if r is None:
            self.skip("Scan tools in /tools", "AVA not running")
            return

        self.check("GET /tools → 200", r.status_code == 200)
        if r.status_code == 200:
            data = r.json()
            # Handle both list and dict responses
            if isinstance(data, list):
                tool_names = [t.get("name", "") for t in data]
            elif isinstance(data, dict):
                tool_names = list(data.keys())
            else:
                tool_names = []

            self.check("'scan_image_trivy' in /tools",  "scan_image_trivy" in tool_names)
            self.check("'scan_system_lynis' in /tools", "scan_system_lynis" in tool_names)
            if tool_names:
                print(f"     {INFO} Total tools registered: {len(tool_names)}")

    def test_react_calls_trivy(self):
        """POST /ask — ReAct loop calls scan_image_trivy when asked"""
        payload = {
            "message": "Scan the alpine:latest Docker image for vulnerabilities using Trivy",
            "use_react": True,
        }
        r = self._req("post", "/ask", body=payload)
        if r is None:
            self.skip("ReAct calls scan_image_trivy", "AVA not running")
            return

        self.check("POST /ask → 200 for trivy scan query", r.status_code == 200)
        if r.status_code == 200:
            data = r.json()
            response_text = str(data).lower()
            # Check that AVA mentioned trivy, CVE, or scanning
            scan_keywords = ["trivy", "cve", "vulnerabilit", "scan", "alpine", "severity"]
            found_keywords = [kw for kw in scan_keywords if kw in response_text]
            self.check(
                "ReAct response references scan results",
                len(found_keywords) >= 2,
                f"Found: {found_keywords}"
            )

    def test_report_created_for_critical(self):
        """Verify that a critical finding auto-generates an incident report."""
        # Check report count before
        r_before = self._req("get", "/reports/stats")
        if r_before is None or r_before.status_code != 200:
            self.skip("Auto-report on critical finding", "Could not get report stats")
            return

        before_total = r_before.json().get("total", 0)

        # Scan an image known to have critical CVEs (python:2.7 is ancient)
        r = self._req("post", "/scan/trivy", body={"image": "python:2.7"},
                      )
        if r is None or r.status_code != 200:
            self.skip("Auto-report on critical finding", "Scan request failed")
            return

        data = r.json()
        if data.get("status") == "error":
            self.skip("Auto-report on critical finding",
                      "Trivy not installed or scan error")
            return

        if data.get("risk_level") not in ("critical", "high"):
            self.skip("Auto-report on critical finding",
                      f"python:2.7 only got risk={data.get('risk_level')} — try different image")
            return

        # Check report count after
        time.sleep(1)
        r_after = self._req("get", "/reports/stats")
        after_total = r_after.json().get("total", 0) if r_after.status_code == 200 else 0
        self.check(
            "Critical scan auto-creates incident report",
            after_total > before_total,
            f"Before={before_total} After={after_total}"
        )

    # ── Summary ──────────────────────────────────────────────────────────────

    def run_all(self, skip_slow: bool = False):
        print(f"\n{'='*60}")
        print("  AVA Day 8 Test Suite — Trivy + Lynis")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}\n")

        print("── Availability ─────────────────────────────────────────────")
        self.test_scan_check()

        print("\n── Auth Gating ──────────────────────────────────────────────")
        self.test_trivy_no_auth()
        self.test_lynis_no_admin()

        print("\n── Input Validation ─────────────────────────────────────────")
        self.test_trivy_missing_image()

        print("\n── Trivy Scan ───────────────────────────────────────────────")
        self.test_trivy_scan_real("alpine:latest")

        print("\n── Lynis Scan ───────────────────────────────────────────────")
        self.test_lynis_scan()

        print("\n── Tool Registry ────────────────────────────────────────────")
        self.test_scan_tools_in_registry()

        if not skip_slow:
            print("\n── ReAct Integration ────────────────────────────────────────")
            self.test_react_calls_trivy()

            print("\n── Auto-Reporting ───────────────────────────────────────────")
            self.test_report_created_for_critical()
        else:
            print(f"\n{SKIP} Skipping slow tests (ReAct + auto-report). Use --full to run.")

        # Summary
        passed = sum(1 for r in self.results if r["passed"] is True)
        failed = sum(1 for r in self.results if r["passed"] is False)
        skipped = sum(1 for r in self.results if r["passed"] is None)
        total  = passed + failed

        print(f"\n{'='*60}")
        print(f"  Results: {passed}/{total} passed  |  {skipped} skipped")
        if failed > 0:
            print(f"\n  Failed tests:")
            for r in self.results:
                if r["passed"] is False:
                    print(f"    {FAIL} {r['name']}  {r['note']}")
        print(f"{'='*60}\n")

        return failed == 0


def main():
    parser = argparse.ArgumentParser(description="AVA Day 8 Test Suite")
    parser.add_argument("--token", required=True, help="JWT admin token")
    parser.add_argument("--full", action="store_true",
                        help="Include slow tests (ReAct loop, auto-report)")
    args = parser.parse_args()

    runner = TestRunner(token=args.token)
    ok = runner.run_all(skip_slow=not args.full)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
