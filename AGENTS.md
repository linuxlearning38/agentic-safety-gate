# AVA Project Notes (v2 Development Contract)

## Serving Contract

AVA must behave like one assistant with one brain:

- AVA decides routing and truth source before any answer is produced.
- AVA knows when to answer exactly from product/runtime truth.
- AVA knows when to use grounded DevOps knowledge.
- AVA knows when to use Qwen for reasoning.
- AVA knows when to block, ask approval, or hand work to the host runner.
- AVA never leaks internal subsystem confusion to the user.
- AVA answers naturally; internal routes stay invisible.

Correct separation:

- AVA is the decision-maker.
- Qwen is a reasoning engine only when AVA selects it.
- Qwen is not the owner of truth, routing, policy, approval, memory, or runtime state.

If AVA does not classify the request first, the serving contract is broken.

## Current Product Line

Current active line on `v2-development`:

- `v2.0.0` - Phase 9 baseline: chat approval to VirtualBox Ubuntu web server, SSH, nginx, hardening, HTTP 200.
- `v2.0.1` - Phase 9.5 operational hardening: startup reliability, runner heartbeat, Redis/session resilience, named data volume, cleaner reboot behavior.
- `v2.1` - Phase 9 Day-2 Operations: manage AVA-created servers after provisioning.

What is intentionally true on this branch:

- AVA can answer DevOps knowledge questions from grounded retrieval.
- AVA can answer AVA self/runtime/architecture questions deterministically.
- AVA can render controlled architecture diagrams for AVA and common DevOps flows.
- AVA can provision VirtualBox Ubuntu web servers through chat approval and the Windows host runner.
- AVA can report connection details, provisioning evidence, live web verification, snapshots, and Day-2 operation status.
- AVA blocks destructive requests and approval-gates medium/high-risk operations.
- Stored evidence must be labeled as stored evidence; live claims must come from live checks.

## Product Boundary

In scope for `v2-development`:

- Exact AVA/self/runtime answers.
- Grounded DevOps knowledge responses.
- Safe local system and container inspection.
- Approval-required handling for medium/high-risk actions.
- Deterministic blocking for destructive requests.
- Phase 9 chat-to-VM provisioning through VirtualBox.
- Phase 9 Day-2 operations on AVA-managed VMs: status, verify, logs, restart, snapshot, rollback, stop/start, and evidence-backed responses.

Out of scope unless explicitly added by a future phase:

- Cloud provider provisioning beyond the local machine.
- Multi-role infrastructure beyond the implemented roles.
- Autonomous destructive changes without approval.
- Public internet exposure unless the user explicitly configures a tunnel or reverse proxy.

## Key Modules

- `web_agent_v2.1_guardrail.py` - single serving contract, `/ask`, UI, response finalization.
- `control/input_router.py` - intent-level controlled routing.
- `control/answer_planner.py` - deterministic controlled answer composition and diagrams.
- `control/evidence_selector.py` - selects evidence for controlled DevOps answers.
- `control/capability_router.py` - deterministic operational capability mapping.
- `control/secure_executor.py` - execution authority and safe tool boundary.
- `control/tool_registry.py` - structured local operational tools.
- `control/security_layer.py` - security logging and integrity.
- `control/approval.py` - approval storage primitives.
- `provisioning/serving.py` - chat-side provisioning and Day-2 session handling.
- `provisioning/runner/host_runner.py` - Windows-native VirtualBox and SSH operation runner.
- `provisioning/runner/job_queue.py` - Redis-backed runner job queue.
- `provisioning/runner/result_writer.py` - runner status and evidence writer.

## Non-Negotiable Rules

- Route first, answer second.
- Keep deterministic routing ahead of freeform fallback.
- Never bypass secure executor or policy checks.
- Never bypass provisioning approval for VM creation.
- Never silently create a second VM on top of an active non-terminal request.
- Never present expired or stored evidence as live truth.
- Never let Qwen invent AVA product facts, tools, credentials, host state, or security posture.
- Keep destructive blocking strict and early.
- Update this file when the serving contract, scope boundary, or durable product truth changes.

## Verification Baseline

Run before closing major serving changes:

- `tests/intelligence_regression.py`
- `tests/serving_contract_regression.py`
- `tests/hybrid_retrieval_regression.py`
- `tests/capability_router_regression.py`
- `tests/security_hardening_regression.py`
- `tests/provisioning_phase6_serving_regression.py`
- `tests/provisioning_phase9_runner_bridge_regression.py`
- `tests/ava_500_robust_audit.py`

Run live checks when the change touches runtime, provisioning, runner, or UI behavior:

- AVA `/health`
- Docker container status
- runner heartbeat
- VirtualBox VM state
- SSH reachability
- HTTP 200 for AVA-created web servers

## Documentation Map

Keep `AGENTS.md` as the short operational truth, not the full history.

- `AGENTS.md` - serving contract, scope boundary, current durable product truth.
- `docs/AVA_V2_PHASE9_RUNNER_BRIDGE_DESIGN.md` - Phase 9 host runner bridge design.
- `docs/AVA_V2_PHASE9_DAY2_OPERATIONS.md` - Phase 9 Day-2 Operations contract.
- `docs/AVA_V1_CURRENT_CAPABILITIES.md` - historical v1 capability sheet.
- release notes and validation docs - version snapshots and test evidence.

Rule of thumb:

- Update `AGENTS.md` when future agents need different behavior.
- Update release notes when a version ships.
- Update capability docs when user-visible behavior changes.
