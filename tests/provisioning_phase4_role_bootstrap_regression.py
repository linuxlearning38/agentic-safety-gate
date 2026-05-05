#!/usr/bin/env python3
"""Regression checks for v2 Phase 4 role and SSH executor contracts."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from provisioning.bootstrap import SSHConnection, SSHExecutor  # noqa: E402
from provisioning.bootstrap import ssh_executor as ssh_executor_module  # noqa: E402
from provisioning.bootstrap.ssh_executor import classify_failure  # noqa: E402
from provisioning.roles.web_server import WEB_SERVER_ROLE, WebServerRole  # noqa: E402


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f" :: {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return condition


def main() -> int:
    failures: list[bool] = []
    failures.extend(
        [
            check("role name is locked", WEB_SERVER_ROLE.name == "web_server"),
            check("role packages are narrow", WEB_SERVER_ROLE.packages == ("nginx", "ufw"), str(WEB_SERVER_ROLE.packages)),
            check("role services are narrow", WEB_SERVER_ROLE.services == ("nginx", "ssh"), str(WEB_SERVER_ROLE.services)),
            check("role exposes only SSH and HTTP", WEB_SERVER_ROLE.ports == ("22/tcp", "80/tcp"), str(WEB_SERVER_ROLE.ports)),
            check("baseline hardening is default", WEB_SERVER_ROLE.hardening_profile == "baseline_linux"),
            check("no generic installer command exists", not any(" install " in step.command and "nginx ufw" not in step.command for step in WEB_SERVER_ROLE.bootstrap_steps)),
            check("SSH is allowed before firewall enable", _step_index("allow_ssh") < _step_index("enable_firewall")),
            check("HTTP is allowed before firewall enable", _step_index("allow_http") < _step_index("enable_firewall")),
            check("nginx starts after firewall rules", _step_index("enable_nginx") > _step_index("enable_firewall")),
        ]
    )

    failures.extend(
        [
            check("apt failure is classified", classify_failure("sudo apt-get update", 100, "") == "package_manager_failed"),
            check("ufw failure is classified", classify_failure("sudo ufw allow 80/tcp", 1, "") == "firewall_failed"),
            check("systemd failure is classified", classify_failure("sudo systemctl enable --now nginx", 1, "") == "service_failed"),
            check("timeout is classified", classify_failure("sleep 999", None, "", timed_out=True) == "command_timeout"),
            check("auth failure is classified", classify_failure("whoami", 255, "Permission denied (publickey)") == "ssh_auth_failed"),
        ]
    )

    no_key_executor = SSHExecutor(SSHConnection(host="127.0.0.1", port=22, username="avaadmin"))
    no_key_result = no_key_executor.run("whoami", timeout_seconds=1)
    failures.extend(
        [
            check("executor returns structured result without key", no_key_result.exit_code == 255),
            check("executor classifies missing key as auth failure", no_key_result.failure_class == "ssh_auth_failed"),
            check("executor result has timing evidence", no_key_result.started_at <= no_key_result.finished_at),
        ]
    )
    failures.extend(test_windows_known_hosts_path_is_quoted())

    role = WebServerRole()
    failures.append(check("role wrapper exposes same definition", role.definition == WEB_SERVER_ROLE))

    failed = len([item for item in failures if not item])
    if failed:
        print(f"\nProvisioning Phase 4 role regression failed: {failed} issue(s)")
        return 1
    print("\nProvisioning Phase 4 role regression passed.")
    return 0


def _step_index(name: str) -> int:
    for index, step in enumerate(WEB_SERVER_ROLE.bootstrap_steps):
        if step.name == name:
            return index
    raise AssertionError(f"step not found: {name}")


def test_windows_known_hosts_path_is_quoted() -> list[bool]:
    captured: list[str] = []
    original_run = ssh_executor_module.subprocess.run

    def fake_run(command, **_kwargs):
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    try:
        ssh_executor_module.subprocess.run = fake_run
        executor = SSHExecutor(
            SSHConnection(
                host="127.0.0.1",
                port=2222,
                username="ava-runner",
                private_key_path=r"C:\Users\mmc\Documents\New project 3\.ava-runner\key",
                known_hosts_path=r"C:\Users\mmc\Documents\New project 3\.ava-runner\known_hosts",
            )
        )
        result = executor.run("whoami", timeout_seconds=1)
    finally:
        ssh_executor_module.subprocess.run = original_run

    known_hosts_arg = next((item for item in captured if item.startswith("UserKnownHostsFile=")), "")
    return [
        check("executor command completed under fake ssh", result.exit_code == 0),
        check("known_hosts path is quoted for OpenSSH parser", known_hosts_arg.startswith('UserKnownHostsFile="')),
        check("known_hosts path keeps folder name with spaces", "New project 3" in known_hosts_arg),
        check("known_hosts path uses forward slashes", "New project 3/.ava-runner/known_hosts" in known_hosts_arg),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
