# AVA v1.0.2 Release Notes

Date: 2026-04-27
Status: Production-ready hardening patch

## What's New in v1.0.2

This is a focused hardening release built on v1.0.1. No scope expansion.

---

### Scope Boundary Enforcement

- Non-DevOps prompts (weather, poetry, math, general chat) now return a clear scope redirect instead of being passed to the LLM.
- `web_agent_v2.1_guardrail.py`: `general_qwen` intent now returns scope redirect response before LLM call.
- Maintains v1 honest positioning as a DevOps-only assistant.
- Prevents AVA from acting outside its defined expertise domain.

### Answer Quality

- `"readiness vs liveness probes"` comparison fixed — was returning noisy or malformed text from raw evidence lines.
- `input_router.py`: canonical normalization for `readiness`/`readiness probes` → `readiness probe`, `liveness`/`liveness probes` → `liveness probe`.
- `answer_planner.py`: `_first_clean_signal_line()` added to pick the first non-noisy evidence line for comparison summaries.
- `_clean_explanation_line()` now strips HTML tags and Markdown links from evidence text.

### Deterministic Mermaid Diagrams

New flow keys added to `answer_planner.py` — all return clean Mermaid, no generic fallback:

| Query trigger | Flow key | Key nodes |
|---|---|---|
| "ava diagram" | `ava_runtime` | ava-agent:5443, PostgreSQL:5432, Redis, OPA, Vault, Ollama |
| "ava kubernetes diagram" | `ava_kubernetes` | Ingress, ava-service, ava-agent Pods, OPA Service, Vault Service |
| "kubernetes diagram" | `kubernetes_runtime` | Client, Ingress, Service, ReplicaSet, Pods, Readiness Probes |
| "devops diagram" | `devops_lifecycle` | Plan→Code→Build→Test→Package→Deploy→Operate→Observe→Improve |
| "netflix diagram" | `netflix_streaming` | Zuul, Kafka, Samza/Mantis, Cassandra, EVCache |
| "docker diagram" | `docker_runtime` | Docker CLI, dockerd, Registry, Image Cache, Container Runtime |
| "ava linux provisioning diagram" | `ava_linux_provisioning` | Explicit experimental status, non-executing on master |

### Documentation

- Added `AVA_V1_CURRENT_CAPABILITIES.md` — honest v1 baseline.
- Defines what v1 does, what it does not do, architecture summary, test coverage, and v2 planning reference.

---

## Validation Snapshot

All tests run against the working tree before tagging v1.0.2:

| Suite | Result |
|---|---|
| `tests/intelligence_regression.py` | PASS |
| `tests/ava_benchmark_suite.py` | PASS |
| `tests/capability_router_regression.py` | PASS |
| `tests/security_hardening_regression.py` | PASS |
| `tests/ava_e2e_live_test.py` | 8/8 PASS |
| Live AVA `/health` | `{"status":"ok"}` |

---

## Compatibility

- No breaking changes from v1.0.1.
- No new dependencies.
- Same Docker rebuild process.
- Same configuration and environment variables.

---

## Known Limitations (Unchanged from v1.0.1)

- Single-host deployment only.
- VirtualBox provisioning remains on `provisioning-v0.1-experimental` branch.
- No multi-server fleet management.
- No cloud provider adapters.
- No automated healing or knowledge refresh.

---

## Roadmap (Reference Only)

- v2.0: Multi-provider provisioning + guarded healing loop.
- v2.1: Knowledge refresh pipeline.
- v2.2: Semantic routing using RAG.
- v3.0: Full LLM-first architecture with structured tool orchestration.
