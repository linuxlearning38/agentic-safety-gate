# AVA v1.0.3 Release Notes

Date: 2026-04-27
Status: Professional polish patch — no functional changes

## What's New in v1.0.3

Focused polish release addressing professional repo presentation issues
identified in post-v1.0.2 audit. No code changes. No new features.

---

### Legal

- Added MIT LICENSE file (was previously missing)
- Project is now properly licensed for use, modification, and distribution

### Security Documentation

- Removed hardcoded default password from README curl example
- Replaced `<YOUR_ADMIN_PASSWORD>` with `<YOUR_ADMIN_PASSWORD>` placeholder
- Added note to change credentials before any non-local deployment

### Repository Hygiene

- Removed `quality_auditor_report.json` (300KB internal runtime artifact) from git tracking
- File was committed before the `.gitignore` rule was added — rule now takes effect
- Reduces clone payload by ~300KB; local file retained

### Continuous Integration

- Replaced echo-only CI stub ("Safety Gate CI") with real test execution
- Two jobs: `syntax` (py_compile all files) and `regression` (4 test suites)
- Triggers on `master` branch for push and pull_request (was: `main`, `day-5-policy-expansion`)
- GitHub Actions green checkmark now reflects actual test results

---

## Validation

All v1.0.2 tests still pass:

| Suite | Result |
|---|---|
| `tests/intelligence_regression.py` | PASS |
| `tests/security_hardening_regression.py` | PASS |
| `tests/ava_benchmark_suite.py` | PASS |
| `tests/capability_router_regression.py` | PASS |

---

## Compatibility

- No breaking changes from v1.0.2
- No new dependencies
- No code changes to any module
- Same Docker rebuild process

---

## Roadmap (Unchanged)

- v1.1: README expansion, CONTRIBUTING.md, SECURITY.md, CHANGELOG.md
- v2.0: Multi-provider provisioning + guarded healing loop
- v3.0: LLM-first architecture with structured tool orchestration
