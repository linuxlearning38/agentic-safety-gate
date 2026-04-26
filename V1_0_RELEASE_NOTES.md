# AVA v1.0 Release Notes

Date: April 26, 2026  
Status: Production-ready for local DevOps knowledge, inspection, and guarded action workflows

## What's New in v1.0

AVA v1.0 consolidates previously shipped capabilities and includes the following readiness bug fixes from v0.x:

- Fix #8.1: docker.service restart routing now triggers approval gate instead of read-only unit inspection response
- Fix #8.2: fix72 host-systemd test mock ordering corrected in intelligence regression
- Fix #8.3: knowledge chunk count synchronized across runtime self-description and documentation
- Fix #8.4: /ask rate limit increased to 60/min for authenticated users (20/min remains fallback for unauthenticated)

## Production-Ready Capabilities

- DevOps knowledge Q&A
- System inspection
- Security observability
- Approval-gated commands
- Deterministic safety blocking
- Audit trail with integrity
- Multi-user RBAC
- JWT authentication
- OPA policy enforcement

## Known Limitations

- Single-host deployment (multi-server in roadmap)
- Local LLM only (no cloud AI integration)
- VirtualBox bridge requires manual Windows setup

## In Active Development (Not in v1.0)

- v2.0: Linux VM provisioning
- v2.0: nginx auto-bootstrap
- v2.0: Full unattended install
- v3.0: Multi-server fleet management

## Migration Notes

- No breaking changes from v0.x
- Run database migrations: none required for this release
- Update environment variables: optional only (`AVA_ASK_RATE_LIMIT` fallback remains compatible)
