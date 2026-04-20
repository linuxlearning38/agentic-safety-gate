# AVA Roadmap

## Operating Rule

Work one fix at a time.

For every fix:

1. Define expected behavior before coding.
2. Implement only that scope.
3. Add or update targeted tests.
4. Run targeted tests.
5. Run full regression tests.
6. Rebuild AVA if serving behavior changed.
7. Run live API/UI checks.
8. Fix any issue found.
9. Move to the next item only after the current item is verified.

Do not batch unrelated fixes.

## Current Baseline

AVA currently has:

- Deterministic identity and self answers.
- Critical destructive-command blocking.
- Approval flow for medium-risk actions.
- Vague diagnostic clarification.
- Unified execution backend.
- Bounded operational classifier.
- Host-risk and suspicious-activity checks.
- Basic cross-signal correlation.
- Follow-up memory for recent operational turns.
- Controlled DevOps definitions for core topics.
- Live 100-question test passing.

Current limitation:

- AVA is a secured DevOps operator wrapper with bounded intelligence, not a fully autonomous SOC/defensive agent yet.

## Phase 0: Checkpoint Current Verified Work

### Goal

Save the current working state after the 100-question live pass.

### Tasks

- Review `git status`.
- Confirm no unrelated files are included.
- Commit current verified changes.
- Keep `.claude/` and `.github/` untouched unless explicitly needed.

### Exit Criteria

- Working tree is clean except intentionally ignored/untracked user files.
- Latest commit message clearly states the verified AVA state.
- `tests/intelligence_regression.py` passes.
- `tests/ava_live_100_question_test.py` passes or the latest passing result is documented.

## Phase 1: Finish Fix #6 RAG/Knowledge Quality

### Fix #6.1: Honest Weak-Evidence Fallback

#### Problem

AVA can still sound confident when retrieval context is weak, noisy, or unrelated.

#### Desired Behavior

If grounded evidence is weak, AVA should say:

```text
I do not have enough grounded evidence to answer this confidently.
```

It may then provide safe next steps, but it must not pretend certainty.

#### Scope

- Improve weak-context detection.
- Add low-confidence fallback response.
- Ensure DevOps questions do not hallucinate from bad chunks.
- Keep general non-DevOps questions on direct Qwen path.

#### Tests

- Bad/noisy retrieval chunk should not produce confident answer.
- Unknown DevOps topic should produce honest fallback.
- Known seeded definitions should still answer normally.
- General Qwen questions should still answer normally.

#### Exit Criteria

- Targeted weak-evidence tests pass.
- Full regression passes.
- Live checks pass for weak, known, and general queries.

### Fix #6.2: Source Selection And Ranking

#### Problem

Different question types need different preferred sources.

#### Desired Behavior

AVA should prefer:

- `seeded_definitions` for core definitions.
- `policies` for definitions and best practices.
- `fixes` for troubleshooting and remediation.
- `patterns` for architecture and design flow.
- `blogs` only as supporting context, not primary truth.

#### Scope

- Add intent-aware source ranking.
- Reduce blog noise.
- Preserve current seeded-definition priority.

#### Tests

- Architecture query prefers `patterns`.
- Troubleshooting query prefers `fixes`.
- Definition query prefers `seeded_definitions` or `policies`.
- Blog-only weak context triggers fallback or lower confidence.

#### Exit Criteria

- Retrieval-source tests pass.
- Existing 100-question live suite still passes.

### Fix #6.3: Deeper DevOps Remediation Answers

#### Problem

Some remediation knowledge answers are still too generic.

#### Desired Behavior

For questions like:

- `How do I safely fix CrashLoopBackOff?`
- `How do I safely fix OOMKilled?`
- `How do I investigate Kubernetes DNS failure?`
- `How do I fix TLS certificate issues?`

AVA should give staged, safe, operator-grade steps:

1. Confirm symptom.
2. Inspect evidence.
3. Identify likely cause.
4. Suggest low-risk fix.
5. Warn about unsafe shortcuts.

#### Scope

- Add controlled remediation answer templates for high-frequency incidents.
- Keep actions advisory unless routed through AVA execution/approval.
- Avoid giving destructive commands as direct instructions.

#### Tests

- CrashLoopBackOff remediation is staged and safe.
- OOMKilled remediation checks memory evidence before increasing limits.
- DNS/TLS remediation avoids blind restarts.
- Destructive remediation suggestions are not emitted.

#### Exit Criteria

- Targeted remediation tests pass.
- Full regression passes.
- Live remediation prompts pass.

### Fix #6.4: Architecture Answer Quality

#### Problem

Some architecture answers can still become generic or lose important system terms.

#### Desired Behavior

Architecture answers should include:

- Components.
- Request flow.
- Data flow.
- Failure points.
- Operational checks.

Diagram answers should include domain-specific nodes, not generic placeholders.

#### Scope

- Improve architecture evidence selection.
- Add more deterministic diagrams for common DevOps flows.
- Improve Mermaid output consistency.

#### Tests

- Kubernetes ingress request flow includes Ingress, Service, Pods, readiness.
- CI/CD flow includes build, test, scan, registry, deploy, observe.
- Terraform flow includes plan, state, apply, drift.
- No generic app-service/data-store diagram when domain terms are known.

#### Exit Criteria

- Architecture regression tests pass.
- Live diagram checks pass.
- Existing diagrams remain valid.

## Phase 2: Host Truth Phase

### Goal

Give AVA safe, bounded access to real host facts instead of only container-visible facts.

### Fix #7.1: Host Telemetry Read-Only Bridge

#### Scope

- Read-only host facts only.
- No host mutation.
- Collect:
  - OS info.
  - disk usage.
  - memory.
  - processes.
  - listening ports.
  - Docker containers.
  - auth logs where available.
  - package state.

#### Exit Criteria

- AVA clearly labels `host_observed` vs `container_observed`.
- No write actions are possible through this bridge.
- Live checks show real host facts.

### Fix #7.2: Host Service Inspection

#### Scope

- Detect whether systemd is available.
- If host systemd is available, inspect services read-only.
- If unavailable, explain limitation clearly.

#### Exit Criteria

- `inspect service nginx` reports host truth when available.
- Container limitation no longer appears as a product weakness.

## Phase 3: Monitoring And Baselines

### Fix #8.1: Baseline Store

#### Scope

- Store normal ports, processes, containers, failed services, auth failure rate.
- Compare current state against previous baseline.

#### Exit Criteria

- AVA can say what changed since last check.
- Baseline changes are auditable.

### Fix #8.2: Scheduled Suspicious-Activity Check

#### Scope

- Periodic check.
- No auto-remediation.
- Report only.

#### Exit Criteria

- AVA can detect drift without user asking.
- Alerts are clear and not noisy.

## Phase 4: Defensive Operator Tools

### Fix #9.1: Approval-Gated Firewall/IP Block

#### Scope

- Suggest blocking suspicious IPs only when evidence exists.
- Require approval.
- Include rollback command.

#### Exit Criteria

- AVA never blocks IPs automatically.
- Approval card includes evidence and rollback.

### Fix #9.2: Container Quarantine

#### Scope

- Detect suspicious container.
- Suggest network disconnect or stop action.
- Require approval.

#### Exit Criteria

- No automatic quarantine.
- Evidence and rollback are shown.

### Fix #9.3: Evidence Snapshot

#### Scope

- Collect read-only incident evidence:
  - processes.
  - ports.
  - auth events.
  - container list.
  - relevant logs.

#### Exit Criteria

- Snapshot is stored with timestamp.
- No destructive action is taken.

## Phase 5: Stronger Self-Heal

### Goal

Move self-heal from known workflow mapping toward a bounded investigation loop.

### Desired Loop

1. Observe.
2. Classify.
3. Run safe checks.
4. Correlate facts.
5. Suggest safest action.
6. Require approval.
7. Execute.
8. Verify outcome.

### Rules

- Qwen may reason only over facts AVA collected.
- Qwen cannot invent tools.
- Qwen cannot bypass approval.
- AVA validates every action.

### Exit Criteria

- Self-heal can verify whether a fix worked.
- Failed remediation produces a safe next diagnostic step.
- Audit trail records the entire loop.

## Phase 6: Security And Release Hygiene

### Fix #10.1: Audit Trail Hardening

- Immutable action records.
- Approval ID.
- User.
- Timestamp.
- Risk level.
- Command/tool.
- Result.

### Fix #10.2: Auth And Secret Hygiene

- Strong JWT secret check.
- Rotate default credentials.
- Avoid committing secrets.
- Document local-dev defaults separately from production.

### Fix #10.3: Release Checklist

- Regression tests.
- Live 100-question test.
- Docker rebuild.
- Health check.
- Known limitations.
- Version tag.

## Recommended Immediate Next Step

Start with:

```text
Phase 0: Checkpoint Current Verified Work
```

Then start:

```text
Fix #6.1: Honest Weak-Evidence Fallback
```

Reason:

- Current work is verified and should be saved.
- Weak-evidence fallback prevents fake confidence.
- It improves product trust without expanding host permissions.

