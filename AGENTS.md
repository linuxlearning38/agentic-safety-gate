# AVA Project Notes (v1.0 Scope Lock)

## Serving Contract

AVA must behave like one assistant with one brain:

- AVA decides routing and truth source.
- Qwen is a reasoning engine only when AVA selects it.
- Security and approval policy are enforced before execution.
- Internal subsystem boundaries are invisible to the user.

## Current State

Current stable line on `master`:

- `v1.0` - scope-locked baseline release.
- `v1.0.1` - stabilization and hardening patch.
- `v1.0.2` - scope enforcement, answer quality fixes, deterministic diagrams.
- `v1.0.3` - professional polish patch (LICENSE, README credential cleanup, real CI, repo hygiene).
- `v1.0.4` - runtime quality patch (vulnerability reporting cleanup, host-risk alignment, controlled diagram routing, rigorous validation).

What is now intentionally true on `master`:

- AVA enforces DevOps-only scope on v1.
- Non-DevOps prompts are redirected instead of answered out of scope.
- Deterministic diagrams exist for key architecture prompts.
- Readiness vs liveness answer quality is fixed and regression-covered.
- Runtime vulnerability scanning is restored and reported more clearly.
- Host-risk and suspicious-activity outputs stay aligned with environment truth.
- Repository presentation now matches shipped behavior more honestly.

## v1.0 Product Boundary (Master)

Master branch is intentionally limited to:

- Exact AVA/self/runtime answers.
- Grounded DevOps knowledge responses.
- Safe system and container inspection tools.
- Approval-required handling for medium/high-risk actions.
- Deterministic blocking for destructive requests.

Out of scope on master:

- VM provisioning/orchestration.
- VirtualBox bridge execution path.
- Unattended OS install + post-install role bootstrap.

Provisioning work is preserved on:

- `provisioning-v0.1-experimental`

## Key Modules

- `web_agent_v2.1_guardrail.py` - serving contract, `/ask`, UI.
- `control/secure_executor.py` - execution authority.
- `control/tool_registry.py` - structured operational tools.
- `control/capability_router.py` - deterministic operational capability mapping.
- `control/input_router.py` - intent-level controlled routing.
- `control/security_layer.py` - security logging and integrity.
- `control/approval.py` - approval storage primitives.

## Rules

- No new scope on master beyond v1.0.
- Do not reintroduce provisioning codepaths into master.
- Keep deterministic routing ahead of freeform fallback.
- Never bypass secure executor or policy checks.
- Keep destructive blocking strict and early.

## Verification Baseline

Run before closing major serving changes:

- `tests/intelligence_regression.py`
- `tests/hybrid_retrieval_regression.py`
- `tests/ava_benchmark_suite.py`
- `tests/capability_router_regression.py`
- `tests/security_hardening_regression.py`
- `tests/ava_e2e_live_test.py`
- `tests/ava_500_robust_audit.py`

## Documentation Map

Keep `AGENTS.md` as the short operational truth, not the full history.

- `AGENTS.md` - serving contract, scope lock, branch intent, latest durable state.
- `docs/AVA_V1_CURRENT_CAPABILITIES.md` - honest current v1 capability sheet.
- `V1_1_WISHLIST.md` - current polish backlog and next-step sequencing for v1.1 work.
- `V1_0_RELEASE_NOTES.md` - v1.0 baseline release snapshot.
- `V1_0_1_RELEASE_NOTES.md` - stabilization and validation snapshot.
- `V1_0_2_RELEASE_NOTES.md` - scope and answer-quality hardening snapshot.
- `V1_0_3_RELEASE_NOTES.md` - repo polish and professionalism snapshot.
- `V1_0_4_RELEASE_NOTES.md` - runtime quality, vulnerability, and validation snapshot.

- `docs/AVA_V2_PHASE9_RUNNER_BRIDGE_DESIGN.md` - Phase 9 design contract: host-side runner
  bridge that closes the chat-to-VM gap discovered in live validation on 2026-05-02.

Rule of thumb:

- Update `AGENTS.md` when the serving contract, scope boundary, or durable product truth changes.
- Update release notes when a version ships.
- Update capability docs when user-visible behavior changes.
