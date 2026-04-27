# AVA v1 Current Capabilities

Date: 2026-04-27
Version: v1.0.2

This document defines what AVA v1 does and does not do on master.
It is the honest baseline for v2 planning.

---

## What AVA v1 Does

### Knowledge Responses

- Kubernetes concepts, objects, and operations (pods, deployments, services, ingress, probes, namespaces, RBAC).
- Docker concepts and runtime operations.
- Linux operations and system inspection.
- CI/CD pipeline architecture and patterns.
- Terraform workflow and state management.
- DevOps lifecycle (plan → code → build → test → deploy → observe → improve).
- Observability stack (Prometheus, Grafana, alerting, log aggregation).
- Comparison queries (readiness vs liveness probe, rolling vs canary, etc.).
- Definition queries for standard DevOps/infrastructure terms.
- Deterministic architecture diagrams (Mermaid syntax):
  - AVA runtime architecture
  - AVA Kubernetes deployment
  - Kubernetes runtime
  - CI/CD pipeline
  - Docker runtime
  - Terraform workflow
  - DevOps lifecycle
  - Netflix streaming (Zuul/Kafka/Cassandra/EVCache reference)
  - AVA Linux provisioning (explicit experimental status)

### Self / Runtime Answers

- "What are you?", "Who made you?", "What can you do?" — deterministic, grounded.
- AVA runtime introspection: container status, port bindings, service health.
- Real-time system checks: disk, memory, CPU, running containers.

### Operational Actions (Guarded)

- **Low-risk read commands** (df, free, ps, docker ps, kubectl get): executed directly, no approval.
- **Medium-risk state-changing commands** (restart pod, install security updates, stop process): routed to approval gate before execution.
- **High-risk / destructive commands** (rm -rf, drop table, kill -9 PID 1): blocked deterministically, no execution path.

### Approval Workflow

- Approval requests stored in PostgreSQL via `control/approval.py`.
- Medium-risk actions require explicit user approval before `secure_executor.py` runs them.
- Policy decisions backed by Open Policy Agent (OPA).

### Scope Boundary Enforcement

- Non-DevOps prompts (weather, poetry, general chat, math, science) return a scope redirect response.
- AVA does not attempt freeform LLM answers for out-of-scope topics.
- Scope redirect text: "AVA v1.0 is scoped to DevOps and infrastructure operations."

---

## What AVA v1 Does NOT Do

| Category | Status |
|---|---|
| VM provisioning (VirtualBox) | Experimental branch only, not on master |
| Multi-server fleet management | Not in v1 |
| Cloud provider adapters (AWS/GCP/Azure) | Not in v1 |
| Automated healing / self-repair | Not in v1 |
| Knowledge refresh pipeline | Not in v1 |
| RAG / semantic retrieval | Not in v1 |
| Multi-turn stateful planning | Not in v1 |
| Prompt/response logging to external stores | Not in v1 |

---

## Architecture Summary

```
User Request
     │
     ▼
web_agent_v2.1_guardrail.py  (Flask/Gunicorn :5443)
     │
     ├─► input_router.py         — intent classification
     ├─► capability_router.py    — operational capability mapping
     ├─► answer_planner.py       — deterministic response construction
     ├─► security_layer.py       — policy and integrity checks
     ├─► secure_executor.py      — guarded command execution
     └─► approval.py             — approval state (PostgreSQL)
          │
          ├── Redis :6379         (cache / session)
          ├── PostgreSQL :5432    (approval state / persistence)
          ├── OPA :8181           (policy decisions)
          ├── Vault :8200         (secrets)
          └── Ollama Host         (local LLM inference)
```

---

## Test Coverage (v1.0.2 baseline)

| Suite | Status |
|---|---|
| `tests/intelligence_regression.py` | PASS |
| `tests/ava_benchmark_suite.py` | PASS |
| `tests/capability_router_regression.py` | PASS |
| `tests/security_hardening_regression.py` | PASS |
| `tests/ava_e2e_live_test.py` | 8/8 PASS |
| `tests/ava_500_robust_audit.py` | 500/500 PASS |
| `tests/ava_live_100_question_test.py` | 35/35 PASS (sample) |

---

## Provisioning Status

VirtualBox provisioning work is preserved on:

- Branch: `provisioning-v0.1-experimental`

It is not executed on master. The AVA linux provisioning diagram references
this work but labels it as experimental and non-executing in the diagram text.

---

## v2 Planning Reference

These are the gaps that define v2 scope. Not committed timelines.

- **v2.0:** Multi-provider provisioning (VirtualBox + cloud) with approval + healing loop.
- **v2.1:** Knowledge refresh pipeline (grounded corpus updates without redeployment).
- **v2.2:** Semantic routing using RAG (replace keyword routing with embedding-based retrieval).
- **v3.0:** Full LLM-first architecture with structured tool orchestration.
