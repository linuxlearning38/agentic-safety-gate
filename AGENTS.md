# AVA Project Notes (v1.0 Scope Lock)

## Serving Contract

AVA must behave like one assistant with one brain:

- AVA decides routing and truth source.
- Qwen is a reasoning engine only when AVA selects it.
- Security and approval policy are enforced before execution.
- Internal subsystem boundaries are invisible to the user.

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

