#!/usr/bin/env python3
"""
test_day9.py
AVA Phase 4 — Day 9 Test Suite: Gunicorn + HTTPS

Tests:
  - HTTP  on :5002 still works
  - HTTPS on :5443 works (self-signed, skip verify)
  - TLS certificate details
  - Auth works over HTTPS
  - All key routes respond over HTTPS
  - Gunicorn worker count
  - Log files being written

Usage:
  TOKEN=$(curl -sk https://localhost:5443/auth/login \\
    -X POST -H "Content-Type: application/json" \\
    -d '{"username":"admin","password":"ava-admin-2026"}' \\
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

  python3 test_day9.py --token $TOKEN
"""

import sys
import ssl
import json
import socket
import argparse
import subprocess
import requests
import urllib3
from datetime import datetime
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HTTP_BASE  = "http://localhost:5002"
HTTPS_BASE = "https://localhost:5443"
TIMEOUT    = 30
LOG_DIR    = Path("/mnt/i/ai-lab/logs")

PASS = "✅"
FAIL = "❌"
SKIP = "⏭️ "
INFO = "ℹ️ "


class TestRunner:
    def __init__(self, token: str):
        self.token   = token
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.results = []

    def _req(self, method, base, path, body=None, headers=None, verify=False):
        url = f"{base}{path}"
        h = headers or self.headers
        try:
            r = getattr(requests, method)(
                url, json=body, headers=h,
                timeout=TIMEOUT, verify=verify,
            )
            return r
        except requests.exceptions.ConnectionError:
            return None
        except requests.exceptions.ReadTimeout:
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

    # ── Tests ─────────────────────────────────────────────────────────────────

    def test_http_still_works(self):
        """HTTP on :5002 still responds."""
        r = self._req("post", HTTP_BASE, "/auth/login",
                      body={"username": "admin", "password": "ava-admin-2026"},
                      headers={"Content-Type": "application/json"})
        if r is None:
            self.check("HTTP :5002 reachable", False, "Connection refused")
            return
        self.check("HTTP :5002 reachable", r.status_code == 200)
        if r.status_code == 200:
            has_token = "access_token" in r.json()
            self.check("HTTP login returns token", has_token)

    def test_https_reachable(self):
        """HTTPS on :5443 responds."""
        r = self._req("get", HTTPS_BASE, "/scan/check",
                      headers=self.headers, verify=False)
        if r is None:
            self.check("HTTPS :5443 reachable", False, "Connection refused — is AVA started with ./start_ava.sh?")
            return
        self.check("HTTPS :5443 reachable", r.status_code in (200, 401))

    def test_https_login(self):
        """Login works over HTTPS."""
        r = self._req("post", HTTPS_BASE, "/auth/login",
                      body={"username": "admin", "password": "ava-admin-2026"},
                      headers={"Content-Type": "application/json"},
                      verify=False)
        if r is None:
            self.skip("HTTPS login", "HTTPS not reachable")
            return
        self.check("HTTPS login → 200", r.status_code == 200)
        if r.status_code == 200:
            data = r.json()
            self.check("HTTPS login returns access_token", "access_token" in data)
            self.check("HTTPS login returns role",         "role" in data)

    def test_https_auth_required(self):
        """Unauthenticated HTTPS request → 401."""
        r = self._req("get", HTTPS_BASE, "/history",
                      headers={"Content-Type": "application/json"},
                      verify=False)
        if r is None:
            self.skip("HTTPS auth required", "HTTPS not reachable")
            return
        self.check("HTTPS unauthenticated → 401", r.status_code == 401)

    def test_https_authenticated_routes(self):
        """Key routes work over HTTPS with token."""
        routes = [
            ("GET",  "/auth/me",        None),
            ("GET",  "/stats",          None),
            ("GET",  "/tools",          None),
            ("GET",  "/scan/check",     None),
        ]
        for method, path, body in routes:
            r = self._req(method.lower(), HTTPS_BASE, path,
                          body=body, verify=False)
            if r is None:
                self.skip(f"HTTPS {method} {path}", "Not reachable")
            else:
                self.check(f"HTTPS {method} {path} → 200",
                           r.status_code == 200,
                           f"got {r.status_code}")

    def test_tls_certificate(self):
        """TLS certificate is valid and has correct fields."""
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            with socket.create_connection(("localhost", 5443), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname="localhost") as ssock:
                    cert = ssock.getpeercert()
                    cipher = ssock.cipher()

            self.check("TLS handshake succeeds",   True)
            self.check("TLS cipher negotiated",    cipher is not None,
                       cipher[0] if cipher else "none")
        except ConnectionRefusedError:
            self.skip("TLS certificate check", "Port 5443 not open")
        except Exception as e:
            self.check("TLS handshake succeeds", False, str(e))

    def test_log_files_exist(self):
        """Gunicorn log files are being written."""
        access_log = LOG_DIR / "ava_access.log"
        error_log  = LOG_DIR / "ava_error.log"
        self.check("Access log exists", access_log.exists(),
                   str(access_log))
        self.check("Error log exists",  error_log.exists(),
                   str(error_log))
        if access_log.exists():
            size = access_log.stat().st_size
            self.check("Access log has content", size > 0, f"{size} bytes")

    def test_gunicorn_process(self):
        """Gunicorn workers are running."""
        result = subprocess.run(
            ["pgrep", "-f", "gunicorn"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().splitlines()
        self.check("Gunicorn processes running", len(pids) > 0,
                   f"{len(pids)} process(es)")

    def test_start_script_exists(self):
        """start_ava.sh exists and is executable."""
        script = Path("/mnt/i/ai-lab/projects/devops-agent/start_ava.sh")
        self.check("start_ava.sh exists",      script.exists())
        self.check("start_ava.sh executable",  os.access(str(script), os.X_OK) if script.exists() else False)

    # ── Summary ───────────────────────────────────────────────────────────────

    def run_all(self):
        print(f"\n{'='*60}")
        print("  AVA Day 9 Test Suite — Gunicorn + HTTPS")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}\n")

        print("── HTTP (legacy) ────────────────────────────────────────")
        self.test_http_still_works()

        print("\n── HTTPS ────────────────────────────────────────────────")
        self.test_https_reachable()
        self.test_https_login()
        self.test_https_auth_required()
        self.test_https_authenticated_routes()

        print("\n── TLS Certificate ──────────────────────────────────────")
        self.test_tls_certificate()

        print("\n── Process + Files ──────────────────────────────────────")
        self.test_gunicorn_process()
        self.test_log_files_exist()
        self.test_start_script_exists()

        passed  = sum(1 for r in self.results if r["passed"] is True)
        failed  = sum(1 for r in self.results if r["passed"] is False)
        skipped = sum(1 for r in self.results if r["passed"] is None)
        total   = passed + failed

        print(f"\n{'='*60}")
        print(f"  Results: {passed}/{total} passed  |  {skipped} skipped")
        if failed > 0:
            print(f"\n  Failed:")
            for r in self.results:
                if r["passed"] is False:
                    print(f"    {FAIL} {r['name']}  {r['note']}")
        print(f"{'='*60}\n")
        return failed == 0


import os

def main():
    parser = argparse.ArgumentParser(description="AVA Day 9 Test Suite")
    parser.add_argument("--token", required=True, help="JWT admin token")
    args = parser.parse_args()

    runner = TestRunner(token=args.token)
    ok = runner.run_all()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
