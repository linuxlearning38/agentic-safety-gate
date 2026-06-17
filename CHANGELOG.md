# Changelog

All notable changes to AVA (Agentic Virtual Assistant) are documented here.

---

## [v2.0.1-provisioning-working] — 2026-06-17

### Phase 9 Milestone: End-to-End VirtualBox VM Pipeline

**What works end-to-end:**

- **Chat-to-VM provisioning** — Full pipeline operational: clone `ubuntu-cloud-image` template → generate cloud-init seed ISO → boot VM → nginx bootstrap → `baseline_linux` hardening role → live HTTP 200 verification → status transitions to `completed`. User types a request in chat; AVA provisions a working web server.

- **Runner readiness fix** — Host runner now trusts the cloud-init/SSH readiness marker written during VM boot rather than failing when the SSH wrapper times out (exit 124). Eliminates false provisioning failures caused by late SSH availability.

- **Phase 9 runner-bridge regression test** — Added `tests/provisioning_phase9_runner_bridge_regression.py` covering the readiness-marker path and preventing regressions in the runner bridge.

- **Live inventory source-of-truth** — "list my servers" now reads live VirtualBox inventory via the host runner. AVA distinguishes AVA-managed VMs from external VMs, reports power state and data freshness. Fixed the always-empty list caused by the previous in-memory-only inventory.

- **Lifecycle operations** — `stop_vm`, `start_vm`, and `verify` commands work end-to-end: approval-gated → dispatched to host runner → executed → evidence returned to chat.

- **Web console** — Live xterm-lite SSH terminal session into AVA-managed VMs via the host runner. Browser never receives SSH private keys or VM passwords.

- **Duplicate-hostname guard** — AVA checks live VirtualBox inventory before provisioning to reject duplicate hostnames.

- **Operational rule: template VM discipline** — The `ubuntu-cloud-image` template must remain powered off at all times. Running it locks its disk and blocks all subsequent clones. AVA enforces this; the template is the clone source, not an operational VM.

**Known follow-ups (not implemented in this release):**

- TODO: Template protection — never offer to delete or stop the `ubuntu-cloud-image` template from chat
- TODO: "restart" verb routing (currently ambiguous between stop+start and OS-level reboot)
- TODO: 127.0.0.1 console scoping for remote/multi-host access
- TODO: Linked-clone-from-snapshot migration (currently full clone)
- TODO: Multipass as planned second provisioning backend

---

## Earlier History

See `git log` for commits prior to v2.0.1. Phase 1–8 work is documented in `docs/AVA_V2_PHASES.md`.
