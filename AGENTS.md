# AVA Project Notes

## Overview
AVA is a secured DevOps assistant built around one serving contract:
- exact answers for AVA/self facts
- grounded DevOps knowledge answers
- secured action handling with execute / approval / block

The user goal is that AVA feels like one assistant with one brain. Internal subsystems must stay invisible to the user.

## What Exists Now
- Unified execution backend via `execute_tool_safe(...)`
- Unified approval queue and normalized approval keys
- Unified callers for `/ask`, tool route, graph, ReAct, and healer
- Structured natural-language operational routing for common safe and medium-risk actions
- Destructive request blocking before knowledge fallback
- Clarification responses for ambiguous operational requests instead of guessing targets
- Deterministic AVA self-routing for identity and runtime questions
- Deterministic Mermaid diagram generation for AVA runtime and DevOps lifecycle prompts
- Architecture retrieval fallback so diagram/architecture answers do not crash on retrieval errors
- Docker inspection via Docker socket, not in-container CLI dependency
- Codex-inspired UI shell with sidebar, recent threads, quick status, approval/result cards, and mobile sidebar behavior

## Key Architecture Decisions
- AVA decides routing. Qwen is only a reasoning engine when AVA chooses it.
- Deterministic routes must not fail just because Ollama is unavailable; dependency gating should only apply to routes that actually need the general LLM path.
- Backend first, cleanup second. Old paths were removed only after live validation.
- No raw shell execution through `shell=True`.
- Medium/high-risk actions go through approval. Critical/destructive actions block.
- Prefer structured tools over raw commands whenever possible.
- Serving-layer guards live in `web_agent_v2.1_guardrail.py`; execution policy lives in `control/secure_executor.py`.

## Important Modules
- `web_agent_v2.1_guardrail.py`
  Main app, `/ask`, UI template, serving contract, operational extraction, clarification, destructive guards.
- `control/secure_executor.py`
  One execution authority for raw commands and tools.
- `control/tool_registry.py`
  Structured tools and subprocess execution helpers.
- `control/input_router.py`
  Deterministic routing for ava_self, architecture, troubleshooting, comparison, definition, follow-up, general_qwen.
- `control/evidence_selector.py`
  Filters and shapes grounded evidence for deterministic answers.
- `control/answer_planner.py`
  Builds deterministic answer plans, including Mermaid diagrams.
- `control/response_composer.py`
  Composes deterministic response text from plans.
- `control/react_loop.py`, `control/command_graph.py`, `control/self_healer.py`
  All must continue routing through the unified executor.
- `control/approval.py`, `control/registry.py`, `control/runtime_paths.py`
  Approval persistence, normalization, runtime-safe storage.
- `control/docker_runtime.py`
  Docker socket inspection for daemon and container state.

## Current Capabilities
- Safe execution:
  - `verify my system`
  - `check docker`
  - `show running containers`
  - `show disk usage`
  - `show memory usage`
- Deterministic self/runtime answers:
  - `what is your name`
  - `who are you`
  - `what model are you running`
- Deterministic diagrams:
  - `create a mermaid diagram of your docker architecture`
  - `create a mermaid diagram of kubernetes, docker, and devops lifecycle`
- Approval flow:
  - `restart docker service`
  - `restart service docker`
  - `rollback deployment nginx`
  - `scale deployment nginx to 5 replicas`
- Safe clarification:
  - `restart my pod`
  - `show me pod logs`
  - `check my service`
  - `rollback my deployment`
  - `scale deployment to 5 replicas`
- Blocking:
  - `rm -rf /`
  - `delete my service`
  - `drop all tables`
  - `truncate my database`

## Current Gaps / Goals
- Kubernetes live actions are not truly end-to-end because there is no real cluster/context yet.
- Knowledge quality is still uneven for some DevOps concepts.
- Diagram generation exists, but diagram routing/rendering should be kept under test.
- Some architecture/knowledge prompts can still return weaker-than-desired answers when they fall back to retrieval-heavy content.
- Broader natural-language operational coverage can still be expanded.
- UI can still be polished further, but current shell is functional.
- The next major product goal is to make AVA a strong Linux autonomous operator before expanding Kubernetes.

## Coding Style / Rules
- Keep changes narrow and practical.
- Do not touch Kubernetes access unless explicitly requested.
- Do not reintroduce multiple execution authorities.
- Do not add brittle LLM-based command guessing back into live execution.
- Prefer deterministic routing/extraction before model fallback.
- Whenever a major change is completed, update this `AGENTS.md` file to reflect the latest project state before ending the work.
- Use `apply_patch` for manual edits.
- Do not revert unrelated user changes.
- Validate with:
  - `tests/intelligence_regression.py`
  - `tests/hybrid_retrieval_regression.py`
  - `tests/ava_benchmark_suite.py`
  - rebuild + live `/ask` checks when serving behavior changes

## Mistakes To Avoid
- Letting destructive operational queries fall through to knowledge/how-to answers
- Guessing missing targets like `my`, `to`, or generic nouns as real service/deployment names
- Routing medium-risk actions as generic errors instead of approval-required results
- Stripping action words too early via query normalization
- Changing backend policy when the issue is only rendering or serving-layer routing
- Assuming diagram failure is backend generation failure without checking frontend Mermaid rendering

## How To Continue In Future Chats
1. Read this file first.
2. Inspect `web_agent_v2.1_guardrail.py` and `control/input_router.py` before proposing routing changes.
3. Preserve the rule: AVA owns routing, security, and truth selection.
4. If behavior is wrong, determine first whether the problem is:
   - serving contract
   - execution backend
   - approval/rendering
   - frontend rendering
   - environment
5. Run the regression suite before rebuilding.
6. Rebuild with:
   - `cd /mnt/i/ai-lab/projects/devops-agent && docker compose up -d --build ava`
7. Verify live with a few exact prompts, not only broad assumptions.

## Useful Prompts For Live Checks
- `what is your name`
- `verify my system`
- `restart docker service`
- `rollback deployment nginx`
- `rollback my deployment`
- `show me pod logs`
- `delete my service`
- `create a mermaid diagram of your docker architecture`
- `create a mermaid diagram of kubernetes, docker, and devops lifecycle`

## Current Working State (Session Memory)
- Timestamp:
  - 2026-04-18 Asia/Calcutta
- Latest changes made:
  - Fixed vague diagnostic queries so AVA now clarifies instead of queuing meaningless approvals:
    - `find problems`
    - `find issues`
    - `check stuff`
    - `something is wrong`
    - bare `diagnose` / `troubleshoot`
  - Added `_is_vague_diagnostic_query(...)` and `_build_vague_diagnostic_clarification()` in `web_agent_v2.1_guardrail.py`
  - Root cause for vague diagnostics: `find problems` and `find issues` were being caught by raw command extraction because `find` is a shell-command starter; clarification had to run BEFORE `extract_explicit_command_request(...)` for this intent family
  - Added regression coverage for vague diagnostic detection and non-regression cases (`run date`, `rm -rf /`, `restart my pod`, `verify my system`, `what is kubernetes`)
  - Rebuilt AVA and verified live from Linux-side `/ask` probes:
    - vague diagnostics now return tool-choice clarification
    - `restart my pod` still asks for a target
    - `rm -rf /` still blocks
    - `run date` still executes
  - Fixed destructive blocking ordering bug in `web_agent_v2.1_guardrail.py`:
    - Root cause: `_resolve_direct_action_query` called `extract_explicit_command_request` BEFORE `_is_single_destructive_request`. Both `echo` and `kill` are in `_RAW_COMMAND_STARTERS`, so "echo \"\" > /etc/passwd" and "kill -9 -1" were extracted as raw commands and routed to `execute_command_secure`. That function ran `analyze_command_security` which returned `approval_required` (credential_access threat for /etc/passwd → medium, unknown command for kill-9-1 → medium) — never critical-blocked.
    - Fix: swapped the two checks in `_resolve_direct_action_query` so `_is_single_destructive_request` runs first. If it matches, return blocked immediately — `execute_command_secure` and `security_layer` never see the query.
    - Added 2 "live path" regression tests that confirm: (1) both queries match `_is_single_destructive_request`, AND (2) `extract_explicit_command_request` would have caught them too — proving the ordering fix is what closes the gap.
    - 184 regression checks pass. Live container ordering confirmed: `if _is_single_destructive_request` at char 335, `explicit_command = extract_explicit_command_request` at char 741 in `_resolve_direct_action_query`.
  - Expanded destructive command blocking in `web_agent_v2.1_guardrail.py`:
    - Root cause: `_is_single_destructive_request` only matched narrow action+target pairs (delete service, drop table, etc.) — mass-deletion, disk format, system file overwrite, permission destruction, system control, and fork bombs all fell through to knowledge routing or approval
    - Fix: replaced the 6-pattern function with an 8-category expanded version covering 30+ patterns; all checked BEFORE knowledge/approval routing
    - Added `_LEARNING_PREFIXES` tuple and `_is_learning_query()` helper — any query beginning with "how", "what", "why", "explain", etc. is treated as informational and NOT blocked, preserving AVA as a knowledge assistant
    - Category 1 — Mass deletion: "delete all <pods|deployments|services|namespaces|nodes|secrets|containers>", "kubectl delete --all", "kill all containers"
    - Category 2 — rm -rf on critical paths: /, /*, /home, /var, /etc, /usr, /boot, /root
    - Category 3 — Disk destruction: "format /dev/", mkfs (any variant), wipefs, shred /dev/, fdisk /dev/, dd if= + of=/dev/
    - Category 4 — Critical system file overwrite: redirect (> or >>) to /etc/passwd, /etc/shadow, /etc/sudoers, /etc/fstab, /etc/hosts, /boot/; also echo-pipe patterns
    - Category 5 — Permissions/auth destruction: chmod 777/000 on system paths, chown -R root on /, usermod -l root, passwd -d root
    - Category 6 — System control: shutdown, halt, poweroff, reboot -f, init 0, init 6, kill -9 -1, killall5
    - Category 7 — Fork bomb: ":(){ :|:& }:;" (checked on raw query before normalization strips special chars)
    - Added 67 regression checks (was 115, now 182): 6 learning-not-blocked, 3 legacy-preserved, 40 new-block cases, 5 safe-preserved cases; all pass
    - Live container verified: all 50 cases correct (40 block + 10 safe)
    - Differentiation is clean: "how do I delete all pods" → not blocked; "delete all pods" → blocked; "please delete all pods" → blocked
  - Fixed identity leak: authorship and safety questions now route deterministically to `ava_self` instead of falling through to Qwen (which identifies itself as built by Alibaba Cloud):
    - Root cause: `AVA_SELF_TOPIC_PATTERNS` in `control/input_router.py` only covered name/model/runtime topics; "who built you", "are you safe to use", and similar questions had no ava_self match and fell to the general_qwen path
    - Fix: added `"authorship"` topic patterns (`who built you`, `who made you`, `who created you`, `who developed you`, `what are you made of`) and `"safety"` topic patterns (`are you safe to use`, `are you safe`, `is ava safe`) to `AVA_SELF_TOPIC_PATTERNS`
    - Fix: added `authorship` and `safety` branches in `build_ava_self_plan` in `control/answer_planner.py` with deterministic canned answers naming Manoj as the builder and describing the approval/blocking safety model
    - Added 11 new regression checks covering routing intent+topic and answer content for both topics; `ava authorship does not say Alibaba` is an explicit regression guard
    - Verified live in container: all 10 identity phrases route correctly; existing `what is your name` and `what model are you running` are unaffected
  - Upgraded `primary_concern` ranking quality in `control/tool_registry.py`:
    - Added `is_novel: bool` field to `_concern_metadata` to distinguish baseline-delta findings from persistent/always-present ones
    - Replaced simple severity+evidence-count sort in `_select_primary_concern` with a fully weighted score: `severity×10 + confidence_bonus(high=3,medium=1,low=0) + evidence_count(max 3) + novelty_bonus(novel=5)`
    - Novel findings (new listeners, new failed services, auth failure spikes vs. baseline) now get +5 and surface above persistent background noise within the same severity tier
    - Persistent background candidates (always-present heuristic alerts, clean baseline) are scored without the novelty bonus — they can still win if their severity is high enough
    - Critical always wins regardless of novelty (40+ base vs. novel medium at best 28 with all bonuses)
    - Added weighted CVE scorer in `scan_host_vulnerabilities`: within the same severity tier, a patchable CVE (fixed_version available) outranks an unpatched one by +2, so the primary concern always recommends a concrete action when one exists
    - Tagged all three baseline-delta concern candidates in `check_suspicious_activity` as `is_novel=True`: new listeners, new failed services, auth failure delta
  - All changes validated:
    - `tests/intelligence_regression.py`: all 50+ checks pass
    - Unit-tested `_select_primary_concern` scoring directly (persistent high > novel medium; novel high > persistent high; critical always wins; empty → None)
    - Unit-tested CVE scorer (CRITICAL+fix beats CRITICAL-no-fix beats HIGH+fix)
    - Live container smoke: `check_suspicious_activity` returns `primary_concern.title="No strong suspicious indicators detected"`, `confidence=high`, `is_novel=False` on a clean system — correct trustworthy result
  - Added operator-intelligence metadata for Linux operator result cards:
    - `primary_concern`
    - ranked evidence
    - `next_action`
    - confidence/severity
  - `scan_host_vulnerabilities` now promotes the top runtime CVE into a deterministic primary concern instead of only listing findings
  - `check_suspicious_activity` now promotes the highest-priority drift/risk signal into a deterministic primary concern instead of only listing alerts
  - Chat command cards now render a dedicated `Primary concern` panel and allow the recommended next action to be run back through AVA
  - Filtered Docker internal DNS listener churn (`127.0.0.11:*`) out of suspicious-listener detection and baseline comparison so container noise does not surface as a false positive
  - Reverified the Linux operator path live after rebuild from the Linux side
  - Deterministic AVA self-identity routing for name/self questions
  - Deterministic Mermaid diagram support for AVA runtime and DevOps lifecycle prompts
  - Architecture retrieval fallback to avoid hard failures on retrieval exceptions
  - Added Linux operator low-risk tools:
    - `check_processes`
    - `check_listening_ports`
    - `check_failed_services`
    - `check_auth_events`
    - `check_updates`
    - `scan_host_vulnerabilities`
    - `check_suspicious_activity`
  - Added Linux operator medium-risk remediation tools:
    - `install_updates`
    - `stop_process`
    - `patch_package`
  - Added Linux operator investigation tools:
    - `inspect_process`
    - `inspect_service`
    - `check_persistence_points`
  - Added natural-language routing for Linux operator prompts such as processes, ports, auth failures, updates, CVEs, and suspicious activity
  - Added natural-language routing for Linux remediation prompts such as:
    - `install security updates`
    - `patch my system`
    - `stop suspicious process 4321`
    - `patch package openssl`
    - `inspect process 4321`
    - `inspect service nginx`
    - `check persistence points`
  - Enriched Linux investigation outputs with suggested next actions
  - Enriched runtime CVE scan output with direct targeted remediation suggestions (`patch package <name>`) plus broad remediation fallback
  - Strengthened suspicious-activity output with alert heuristics for unusual listeners and review-worthy process commands
  - Added structured metadata pass-through from tool results -> secure executor -> `/ask` command result payload
  - Added baseline-aware suspicious activity comparison for listening endpoints
  - Expanded suspicious-activity baselines to compare:
    - authentication failure count
    - failed service names
  - Added structured remediation candidates to runtime CVE scan metadata
  - Added structured suggested-action metadata to process/service/suspicious-activity inspections
  - Added chat UI rendering for Linux-operator metadata so executed command cards can now surface:
    - alerts
    - suggested actions
    - remediation candidates
    - newly observed listening endpoints
    - newly failed services
    - auth failure trend deltas
  - Added remediation-candidate action buttons in chat cards so Linux findings can be sent back through AVA as normal prompts
  - Investigated Linux operator failures and fixed the root causes:
    - Added `iproute2` and `net-tools` to the runtime image so `show listening ports` can use `ss`/`netstat`
    - Reordered `/ask` handling so explicit/operational Linux prompts run before troubleshooting/KB routes
    - Fixed multi-part Linux prompts so comma-separated operational requests are split and processed part-by-part through the same serving contract
    - Fixed `inspect process <pid>` so missing PIDs return a clear `No process found for PID ...` error
    - Changed systemd-dependent service inspection paths to report container limitations honestly instead of failing or pretending systemd is available
    - Removed false suspicious-activity findings caused by systemd being unavailable inside the AVA container
    - Improved service-inspection guidance so it no longer loops back to the same command pointlessly
  - Added regression coverage for the new Linux operator phrases
  - `AGENTS.md` updated to reflect current serving-contract state
- Root causes investigated:
  - Vague diagnostic intent without a concrete target had no first-class clarification path
  - `find problems` / `find issues` leaked specifically because `find` is a raw-command starter and raw extraction was running before vague-diagnostic clarification
  - Linux operator routing was losing to controlled troubleshooting/knowledge branches because operational execution happened too late in `/ask`
  - Multi-question handling only built knowledge answers and did not execute operational tools for each sub-query
  - The runtime image lacked port-inspection binaries
  - Some Linux tools assumed a systemd host, but AVA currently runs inside a non-systemd container
  - `inspect_process` surfaced raw `ps` failure text instead of turning it into a user-facing diagnosis
- Outcome verified:
  - `find problems`, `find issues`, `check stuff`, and `something is wrong` now return clarification with suggested check paths instead of approval
  - Existing behavior stayed intact for:
    - `restart my pod` -> asks for deployment name
    - `rm -rf /` -> blocked
    - `run date` -> executes
  - `show listening ports` now executes successfully
  - `check failed services` now routes into the Linux operator path and returns an honest container/systemd limitation message
  - comma-separated Linux operator prompts now return `type: "multi"` with per-part command results
  - `inspect process 1234` now returns `No process found for PID 1234`
  - `inspect service nginx` now explains the container limitation and gives non-recursive next steps
- Current bugs:
  - Knowledge quality is still uneven for some retrieval-heavy DevOps answers
  - Kubernetes live actions are not end-to-end verified because no real cluster/context exists
  - Some broader natural-language operational phrases may still need clarification/coverage expansion
  - Live Windows PowerShell HTTPS smoke calls to `/ask` can intermittently hit transport EOF; this has not shown evidence of an AVA container crash
  - Linux vulnerability scanning depends on Trivy availability and scans the AVA runtime filesystem, not an external host
  - Linux vulnerability/CVE remediation is more structured now and can be resubmitted through AVA from chat cards, but it is still package-manager based and local rather than host-fleet aware
  - Suspicious-activity comparison now covers listeners, auth-failure volume, and failed service names, but not broader historical baselines for process behavior or service health over time
  - Service-state inspection is intentionally limited in the AVA container because systemd is not running there; host-level service checks need a host context, not more container patching
  - Some deterministic tool metadata paths are harder to validate from Windows-side smoke probes because local transport/tool availability can differ from in-container execution
- Current focus:
  - Preserve one strict serving contract where AVA chooses the answer mode before any response is produced
  - Keep deterministic/self/security paths invisible and consistent for the user
  - Expand coverage without reintroducing stitched-together routing behavior
  - Shift AVA from “tool wrapper” toward operator intelligence by ranking findings, naming the top concern, and recommending the next best action
  - Reduce meaningless approval prompts when the user intent is diagnostic but underspecified
- Next steps:
  - Improve the weakest knowledge-answer cases
  - Replace more pattern-heavy operational routing with a hybrid intent-classifier path instead of continuing phrase explosion
  - Add cross-signal reasoning so AVA correlates multiple Linux/operator signals before choosing the top concern
  - Expand baseline/history-aware comparisons further to high-risk processes and service health over time (process ancestry, unusual CPU bursts)
  - Add richer service/process remediation guidance tied to actual findings
  - Add richer remediation actions from findings beyond package prompts, including stronger service/process follow-ups
  - Expand Linux operator coverage with package/service specific clarification prompts
  - Only attempt host-level service inspection when AVA is given a real host/systemd context; do not treat this as a container bug
  - Continue UI polish only when it does not affect backend behavior
  - Always update this section after major changes

## Next Planned Phase: Linux Autonomous Operator
- Timestamp:
  - 2026-04-14 Asia/Calcutta
- Goal:
  - Make AVA strong on Linux administration and security operations before Kubernetes.
- What we have done already:
  - Unified serving contract and execution backend
  - Approval-aware action handling and critical blocking
  - Safe system and Docker inspection
  - Clarification instead of guessing for ambiguous operational requests
  - Deterministic AVA self-knowledge and deterministic Mermaid support
  - Initial Linux operator read-only inspection tools and phrase routing
- What AVA should become in this phase:
  - A Linux operations assistant that can inspect, diagnose, detect suspicious activity, identify vulnerabilities/CVEs, explain impact, and propose or queue remediations.
- Planned capabilities:
  - Read-only Linux diagnostics:
    - processes
    - listening ports
    - failed services
    - package inventory
    - update availability
    - disk hotspots
    - auth/security log review
  - CVE and vulnerability triage:
    - package and service version collection
    - vulnerability scan summary
    - CVE listing with severity
    - exact remediation steps by package/service
  - Suspicious activity checks:
    - repeated SSH/auth failures
    - unusual processes
    - unexpected listening ports
    - persistence points like cron/systemd entries
  - Approval-aware remediation:
    - restart service
    - install security updates
    - patch package
    - stop suspicious process
- Initial implementation order:
  - Add Linux diagnostic tools
  - Add vulnerability/CVE triage path
  - Add suspicious-activity detection path
  - Add approval-aware remediation tools
  - Add live prompts and regression coverage
- Current phase status:
  - Linux diagnostics: started
  - CVE/vulnerability triage: started
  - Suspicious activity checks: started
  - Approval-aware Linux remediation: started
  - Investigation tooling: started
  - Remediation suggestions from findings: started
  - Structured metadata/result contract for Linux operator findings: started
