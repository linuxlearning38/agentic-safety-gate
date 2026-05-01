# AVA v2 Phase 4 Design Note

Date: 2026-05-01  
Branch: `v2-development`  
Status: Implemented after live VirtualBox web bootstrap smoke

## Purpose

Phase 4 is where AVA stops being only a state machine and starts changing a
real Ubuntu guest over SSH.

That makes it different from Phases 0-3. This document locks the implementation
contract before code starts so the web role does not become a pile of shell
strings and hopeful success messages.

Scope remains:

- provider: `VirtualBox`
- OS: `Ubuntu`
- role: `web_server`
- hardening profile: `baseline_linux`
- service: `nginx`

Out of scope:

- second role
- generic package installer
- Ansible integration
- full CIS benchmark
- automatic rollback beyond clear failure reporting and later Phase 7 cleanup

## Phase 4 Output

Phase 4 should produce:

- `provisioning/roles/base.py`
- `provisioning/roles/web_server.py`
- `provisioning/bootstrap/ssh_executor.py`
- regression tests for role contract, hardening contract, and executor behavior
- one live smoke that bootstraps nginx on a cloned Ubuntu cloud-image VM

Phase 4 is complete only when a real clone can be accessed over SSH, configured
with the web role, and serve HTTP 200 through the existing NAT HTTP forwarding.

Implemented files:

- `provisioning/bootstrap/ssh_executor.py`
- `provisioning/roles/base.py`
- `provisioning/roles/web_server.py`
- `tests/provisioning_phase4_role_bootstrap_regression.py`
- `tests/virtualbox_web_server_bootstrap_smoke.py`

## Baseline Linux Profile

`baseline_linux` must be deterministic. For `v2.0.0`, it means exactly this:

1. Confirm the session is running as a sudo-capable non-root user.
2. Ensure package metadata can be refreshed with a bounded timeout.
3. Install required role packages only.
4. Ensure OpenSSH remains enabled and running.
5. Configure UFW if available, installing it only when the package manager path
   is already healthy.
6. Allow SSH before enabling or reloading the firewall.
7. Allow the role-defined HTTP port.
8. Enable UFW only after SSH and HTTP allow rules are present.
9. Enable and start the role-defined service.
10. Verify that hardening did not break SSH, nginx, or HTTP reachability.

`baseline_linux` does not include in `v2.0.0`:

- changing SSH ports
- disabling password auth automatically
- fail2ban
- auditd
- kernel/sysctl hardening
- unattended upgrades
- CIS benchmark enforcement
- user deletion or account lockout

Those can be later profiles. They are deliberately excluded because the first
slice must prove safe role-aware hardening without locking AVA out of the VM.

## Role And Hardening Sequence

The sequence is:

1. Preflight over SSH.
2. Refresh package metadata.
3. Install role packages.
4. Apply role-aware firewall rules.
5. Enable firewall.
6. Enable and start role services.
7. Run role-local verification.

Reason:

- Package install should happen before strict firewall enablement so AVA can
  still recover from package-manager failures.
- SSH must be allowed before any firewall enablement.
- The HTTP port must be allowed before nginx verification.
- Service start happens after firewall rules so the final state matches what
  the user will actually use.

This means Phase 4 uses "install then harden then verify" for the first slice,
where "harden" is limited to role-aware firewall and service-safe baseline
checks.

## Web Server Role Contract

`web_server` defines:

- packages: `nginx`, `ufw`
- services: `nginx`, `ssh`
- ports: `22/tcp`, `80/tcp`
- firewall profile: `web_public`
- hardening profile: `baseline_linux`

Bootstrap commands should be idempotent where practical:

- `sudo apt-get update`
- `sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nginx ufw`
- `sudo ufw allow 22/tcp`
- `sudo ufw allow 80/tcp`
- `sudo ufw --force enable`
- `sudo systemctl enable --now nginx`
- `sudo systemctl is-active nginx`

The role must not install unrelated packages or expose unrelated ports.

## SSH Executor Contract

The SSH executor must not return raw strings as the only signal.

Each command result must include:

- `command`
- `exit_code`
- `stdout`
- `stderr`
- `duration_seconds`
- `started_at`
- `finished_at`
- `timed_out`
- `failure_class`

Proposed result shape:

```python
{
    "command": "sudo apt-get update",
    "exit_code": 0,
    "stdout": "...",
    "stderr": "...",
    "duration_seconds": 3.42,
    "started_at": "2026-05-01T13:10:00+00:00",
    "finished_at": "2026-05-01T13:10:03+00:00",
    "timed_out": False,
    "failure_class": None,
}
```

Executor inputs:

- host
- port
- username
- private key path or temporary password
- command
- timeout seconds
- redact patterns

For Phase 4 smoke testing, SSH-key auth is acceptable because the existing
cloud-init access smoke already seeds a test key for automation. The product
UX still remains temporary username/password with forced password change.

## Failure Classification

The executor should classify failures into a small fixed vocabulary:

- `ssh_connect_timeout`
- `ssh_auth_failed`
- `command_timeout`
- `package_manager_failed`
- `service_failed`
- `firewall_failed`
- `verification_failed`
- `unknown`

Classification rules:

- SSH cannot connect within timeout: `ssh_connect_timeout`
- SSH returns permission/auth errors: `ssh_auth_failed`
- command exceeds timeout: `command_timeout`
- `apt-get` exits non-zero: `package_manager_failed`
- `systemctl enable/start/is-active nginx` exits non-zero: `service_failed`
- `ufw` exits non-zero: `firewall_failed`
- HTTP check or service verification fails: `verification_failed`
- otherwise: `unknown`

Phase 4 should report the failure clearly but should not implement full cleanup
logic. Rollback and retain-for-debug behavior belongs to Phase 7.

## Verification Boundary

Phase 4 must verify enough to prove the role works, but full persistence belongs
to Phase 5.

Phase 4 live smoke must prove:

- SSH command execution works
- package refresh/install path works
- nginx is installed
- nginx is active
- UFW allows SSH and HTTP without breaking the session
- HTTP returns status 200 through the existing host NAT forwarding

## Implementation Order

1. Add role dataclasses and `web_server` contract.
2. Add SSH executor result contract and command execution wrapper.
3. Add unit regression for role contract and failure classification.
4. Add bootstrap runner for `web_server`.
5. Add live smoke that clones the Ubuntu template, injects access, bootstraps
   nginx, verifies HTTP 200, and destroys the clone.
6. Update `docs/AVA_V2_PHASES.md` with implemented Phase 4 behavior after the
   smoke passes.

## Go / No-Go Rule

Do not start Phase 4 implementation unless this design is accepted as the
contract.

If implementation discovers that one of these assumptions is wrong, stop and
update this design note before continuing.

## Verified Live Result

Verified on 2026-05-01 with:

- `python tests\virtualbox_web_server_bootstrap_smoke.py`

Observed result:

- cloud-init seed ISO created
- clone created from `ubuntu-cloud-image`
- cloud-init seed attached
- VM started headlessly
- SSH became reachable through `127.0.0.1:2222`
- cloud-init marker verified
- sudo preflight passed
- `apt-get update` passed
- `nginx` and `ufw` installed
- UFW allowed `22/tcp`
- UFW allowed `80/tcp`
- UFW enabled
- nginx enabled and started
- guest-local `systemctl is-active nginx` passed
- guest-local HTTP check passed
- host NAT HTTP returned `HTTP 200` on `127.0.0.1:8080`
- smoke VM was destroyed after verification
