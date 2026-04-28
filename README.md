# AVA v1.0.4 - Local DevOps Knowledge Assistant

AVA is a local-first DevOps assistant focused on grounded answers, safe operations, and approval-aware execution.

## Current Stable Scope (Master Branch)

Master is intentionally scoped to the v1.0 product boundary:

- Grounded DevOps Q&A over local ChromaDB knowledge.
- Safe operational inspection (system, Docker, services, ports, disk, memory).
- Deterministic destructive-action blocking.
- Approval-required handling for medium/high-risk actions.
- JWT auth, RBAC, OPA policy checks, and audit logging.

Not included on master:

- Linux VM provisioning and VirtualBox orchestration.
- Unattended installer automation.
- In-guest nginx bootstrap pipeline.

Those capabilities are preserved on `provisioning-v0.1-experimental`.

## Architecture Summary

- Model: `qwen2.5:14b` via Ollama (local).
- Retrieval: ChromaDB (runtime count exposed by AVA self-introduction).
- API/UI: Flask + Gunicorn over HTTPS (`:5443`).
- Security: OPA policy, JWT, RBAC, rate limiting, tamper-evident audit log.

## Quick Start

```bash
docker compose up -d --build ava
curl -sk https://localhost:5443/health
```

Get a token:

> **Note:** Replace `<YOUR_ADMIN_PASSWORD>` with your actual admin password.
> Default credentials are set in `.env` and **must** be changed before any
> non-local deployment.

```bash
curl -sk https://localhost:5443/auth/login \
  -X POST -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<YOUR_ADMIN_PASSWORD>"}'
```

Ask AVA:

```bash
curl -sk https://localhost:5443/ask \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"query":"check docker"}'
```

## Core Endpoints

- `GET /health`
- `POST /auth/login`
- `POST /ask`
- `POST /tools/<tool_name>/run`
- `POST /react/run`
- `GET /security/stats`
- `GET /security/audit`

## Release Notes

Current release line:

- `V1_0_RELEASE_NOTES.md`
- `V1_0_1_RELEASE_NOTES.md`
- `V1_0_2_RELEASE_NOTES.md`
- `V1_0_3_RELEASE_NOTES.md`
- `V1_0_4_RELEASE_NOTES.md`

Current capabilities reference:

- `docs/AVA_V1_CURRENT_CAPABILITIES.md`

## Experimental Provisioning Branch

Provisioning work is preserved separately:

- Branch: `provisioning-v0.1-experimental`
- Marker doc: `PROVISIONING_BRANCH_README.md` (on that branch)
