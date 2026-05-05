"""Narrow web_server role for AVA v2.0.0."""

from __future__ import annotations

from dataclasses import dataclass

from provisioning.bootstrap import SSHCommandResult, SSHExecutor

from .base import BootstrapCommand, RoleDefinition


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
            command="whoami && sudo -n true && systemctl is-active ssh >/dev/null 2>&1",
            timeout_seconds=30,
            failure_class="ssh_auth_failed",
        ),
        BootstrapCommand(
            name="network_ready",
            command=(
                "for i in $(seq 1 36); do "
                "getent hosts archive.ubuntu.com >/dev/null 2>&1 && "
                "getent hosts security.ubuntu.com >/dev/null 2>&1 && exit 0; "
                "sleep 5; "
                "done; "
                "echo 'DNS resolution not ready for Ubuntu package mirrors' >&2; "
                "resolvectl status 2>/dev/null || cat /etc/resolv.conf; "
                "exit 1"
            ),
            timeout_seconds=210,
            failure_class="package_manager_failed",
        ),
        BootstrapCommand(
            name="package_update",
            command=(
                "for i in 1 2 3; do "
                "sudo apt-get update && exit 0; "
                "sleep 10; "
                "done; "
                "sudo apt-get update"
            ),
            timeout_seconds=360,
            failure_class="package_manager_failed",
        ),
        BootstrapCommand(
            name="install_web_packages",
            command=(
                "for i in 1 2 3; do "
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --fix-missing nginx ufw && exit 0; "
                "sudo apt-get update || true; "
                "sleep 10; "
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

    def bootstrap(self, executor: SSHExecutor) -> list[SSHCommandResult]:
        """Run bootstrap and role-local verification steps in locked order."""

        results: list[SSHCommandResult] = []
        for step in (*self.definition.bootstrap_steps, *self.definition.verification_checks):
            result = executor.run(step.command, timeout_seconds=step.timeout_seconds)
            if result.exit_code != 0 and result.failure_class is None:
                result.failure_class = step.failure_class
            results.append(result)
            if result.exit_code != 0:
                break
        return results
