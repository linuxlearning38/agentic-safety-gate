"""Narrow web_server role for AVA v2.0.0."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from provisioning.bootstrap import SSHCommandResult, SSHExecutor

from .base import BootstrapCommand, RoleDefinition


GUEST_PREFLIGHT_COMMAND = (
    "current_user=$(id -un 2>/dev/null || true); "
    "if [ \"$current_user\" != 'ava-runner' ]; then "
    "echo \"Guest readiness short check failed: unexpected_user:$current_user\" >&2; exit 1; "
    "fi; "
    "if ! timeout 5s sudo -n true >/dev/null 2>&1; then "
    "echo 'Guest readiness short check failed: passwordless_sudo_not_ready' >&2; exit 1; "
    "fi; "
    "if systemctl is-active --quiet ssh 2>/dev/null || "
    "systemctl is-active --quiet sshd 2>/dev/null || "
    "pgrep -x sshd >/dev/null 2>&1; then "
    "echo AVA_GUEST_READY; exit 0; "
    "fi; "
    "echo 'Guest readiness short check failed: ssh_service_not_active' >&2; "
    "echo \"user=$(id -un 2>/dev/null || true)\" >&2; "
    "timeout 5s sudo -n true >/dev/null 2>&1 && "
    "echo 'sudo_nopasswd=ok' >&2 || echo 'sudo_nopasswd=failed' >&2; "
    "echo \"ssh_active=$(systemctl is-active ssh 2>/dev/null || true)\" >&2; "
    "echo \"sshd_active=$(systemctl is-active sshd 2>/dev/null || true)\" >&2; "
    "pgrep -a sshd >&2 || true; "
    "cloud-init status --long >&2 || true; "
    "exit 1"
)


WEB_SERVER_ROLE = RoleDefinition(
    name="web_server",
    packages=("nginx", "ufw"),
    services=("nginx", "ssh"),
    ports=("22/tcp", "80/tcp"),
    firewall_profile="web_public",
    hardening_profile="baseline_linux",
    bootstrap_steps=(
        BootstrapCommand(
            name="preflight",
            command=GUEST_PREFLIGHT_COMMAND,
            timeout_seconds=90,
            failure_class="guest_readiness_timeout",
        ),
        BootstrapCommand(
            name="network_ready",
            command=(
                "for i in $(seq 1 72); do "
                "getent hosts archive.ubuntu.com >/dev/null 2>&1 && "
                "getent hosts security.ubuntu.com >/dev/null 2>&1 && exit 0; "
                "if [ $((i % 6)) -eq 0 ]; then "
                "sudo resolvectl flush-caches >/dev/null 2>&1 || true; "
                "sudo systemctl restart systemd-resolved >/dev/null 2>&1 || true; "
                "fi; "
                "sleep 5; "
                "done; "
                "echo 'DNS resolution not ready for Ubuntu package mirrors' >&2; "
                "resolvectl status 2>/dev/null || cat /etc/resolv.conf; "
                "exit 1"
            ),
            timeout_seconds=420,
            failure_class="package_manager_failed",
        ),
        BootstrapCommand(
            name="package_update",
            command=(
                "for i in 1 2 3 4 5 6; do "
                "sudo apt-get update && exit 0; "
                "sudo resolvectl flush-caches >/dev/null 2>&1 || true; "
                "sleep 15; "
                "done; "
                "sudo apt-get update"
            ),
            timeout_seconds=360,
            failure_class="package_manager_failed",
        ),
        BootstrapCommand(
            name="install_web_packages",
            command=(
                "for i in 1 2 3 4 5; do "
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --fix-missing nginx ufw && exit 0; "
                "sudo apt-get update || true; "
                "sleep 15; "
                "done; "
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --fix-missing nginx ufw"
            ),
            timeout_seconds=480,
            failure_class="package_manager_failed",
        ),
        BootstrapCommand(
            name="allow_ssh",
            command="sudo ufw allow 22/tcp",
            timeout_seconds=60,
            failure_class="firewall_failed",
        ),
        BootstrapCommand(
            name="allow_http",
            command="sudo ufw allow 80/tcp",
            timeout_seconds=60,
            failure_class="firewall_failed",
        ),
        BootstrapCommand(
            name="enable_firewall",
            command="sudo ufw --force enable",
            timeout_seconds=60,
            failure_class="firewall_failed",
        ),
        BootstrapCommand(
            name="enable_nginx",
            command="sudo systemctl enable --now nginx",
            timeout_seconds=60,
            failure_class="service_failed",
        ),
    ),
    verification_checks=(
        BootstrapCommand(
            name="verify_nginx_active",
            command="systemctl is-active nginx",
            timeout_seconds=30,
            failure_class="verification_failed",
        ),
        BootstrapCommand(
            name="verify_http_local",
            command="curl -fsS http://127.0.0.1/ >/dev/null",
            timeout_seconds=30,
            failure_class="verification_failed",
        ),
    ),
)


@dataclass(slots=True)
class WebServerRole:
    """Apply the v2.0.0 web_server role through the SSH executor."""

    definition: RoleDefinition = WEB_SERVER_ROLE

    def bootstrap(
        self,
        executor: SSHExecutor,
        heartbeat: Callable[[], None] | None = None,
    ) -> list[SSHCommandResult]:
        """Run bootstrap and role-local verification steps in locked order."""

        results: list[SSHCommandResult] = []
        for step in (*self.definition.bootstrap_steps, *self.definition.verification_checks):
            if heartbeat is not None:
                heartbeat()
            result = executor.run(step.command, timeout_seconds=step.timeout_seconds)
            if heartbeat is not None:
                heartbeat()
            if step.name == "preflight" and "AVA_GUEST_READY" in (result.stdout or ""):
                result.exit_code = 0
                result.timed_out = False
                result.failure_class = None
            if result.exit_code != 0 and result.failure_class is None:
                result.failure_class = step.failure_class
            results.append(result)
            if result.exit_code != 0:
                break
        return results
