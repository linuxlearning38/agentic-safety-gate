# AVA v2 Live Provisioning Validation - 2026-05-02

Timestamp: 2026-05-02 13:15:14 +05:30

Branch: `v2-development`

Live access URL: `https://enabled-pins-begun-smooth.trycloudflare.com`

## Purpose

Validate the guided AVA v2 provisioning conversation from the live AVA service and document:

- What was tested.
- What failed in the live browser/API flow.
- What was fixed.
- What still remains before live chat can create a VirtualBox VM by itself.
- Which regression checks passed after the fixes.

## Original Live Issue

The user tested the guided prompt:

```text
I want a web server
3 cpu, 8gb ram, 60 gb disk
```

Earlier behavior was wrong:

- AVA asked for `cpu`, `ram_gb`, and `disk_gb`.
- The user provided CPU/RAM/disk.
- AVA still asked for `cpu`.

Root cause:

- The serving normalizer treated leading numeric text like `3 cpu` as if it might be copied list numbering.
- That stripped the leading `3` before the provisioning parser saw it.
- The parser then saw only `cpu`, so CPU was considered missing.

Fix already applied before this validation:

- Preserve leading numeric infrastructure specs such as `3 cpu`, `3 CPU`, `8gb ram`, and `60 gb disk`.
- Add regression coverage for uppercase and lowercase CPU/RAM/disk formats.

## Approval Flow Issue

The user also tried:

```text
approve - 7e9cc680
```

Earlier behavior was wrong:

- AVA did not understand the approval reply when the ID was written with a dash separator.
- It routed the prompt to the generic out-of-scope response.

Root cause:

- Chat approval parsing only accepted direct formats such as `approve 7e9cc680`.
- It did not accept `approve - 7e9cc680`.

Fix already applied before this validation:

- Accept exact approval commands with optional separators: `approve <id>`, `approve - <id>`, `approve: <id>`, and `approve # <id>`.
- Keep exact-ID approval required. Vague prompts such as `approve it` are intentionally not enough.

## VM Visibility Issue

The user approved the plan and saw temporary credentials, but no new VM appeared in VirtualBox.

Observed behavior:

- AVA collected the desired state.
- AVA queued approval.
- AVA accepted the exact approval ID.
- AVA issued temporary credentials.
- No new VM was created in VirtualBox from the live chat flow.

Root cause:

- The live AVA chat path currently handles conversation, desired-state collection, approval, credential issuance, first-login confirmation, and hardening selection.
- It does not yet execute the approved plan through a host-side VirtualBox runner.
- The Docker container cannot directly create Windows VirtualBox VMs by itself.
- The real VirtualBox creation currently works through the host-side end-to-end runner, not through the browser chat approval path.

Fix applied during this validation:

- AVA now states the execution boundary clearly after approval.
- AVA no longer implies that approval itself creates a VM.
- AVA says the VM appears only after the host-side VirtualBox runner executes the approved plan.

## Follow-Up Prompt Issue Found During Live Test

After the approved conversation reached hardening, these prompts were tested:

```text
verify the web server
show me the provisioning status
what did you do and what evidence do you have?
```

Earlier live behavior was wrong:

- `verify the web server` routed to generic host service inspection for a service named `web`.
- `show me the provisioning status` fell into generic cloud/provisioning knowledge.
- `what did you do and what evidence do you have?` fell into the v1 out-of-scope message.

Root cause:

- Once the provisioning session reached the `bootstrapping` phase, the serving layer did not handle verification/status/evidence follow-ups.
- Those prompts escaped the active provisioning session and were routed by the generic AVA router.

Fix applied during this validation:

- Added provisioning-session follow-up handling for:
  - `verify the web server`
  - `show me the provisioning status`
  - `what did you do and what evidence do you have?`
- Responses now stay attached to the active provisioning session.
- Responses honestly report whether a VM instance is attached.
- If no VM exists yet, AVA explains that only conversation/approval evidence exists and the host-side VirtualBox runner is still required.

Files changed:

- `provisioning/serving.py`
- `tests/provisioning_phase6_serving_regression.py`

## Rebuild

The AVA container was rebuilt after the serving fix.

Health check after rebuild:

```text
{"dependencies":{"ollama":true,"opa":true,"redis":true},"status":"ok"}
```

Docker status after rebuild:

```text
ava-agent Up (healthy)
agent_opa Up
agent_postgres Up
ava_docker_socket_proxy Up
agent_redis Up
agent_vault Up
```

## Live Guided Provisioning Test

Tested against live `/ask` after rebuild.

### Prompt 1

```text
I want a web server in Ubuntu
```

Result: PASS

AVA asked for missing specs:

```text
I need these specs before I can plan the VM: cpu, ram_gb, disk_gb.
```

### Prompt 2

```text
2 CPU, 4 GB RAM, 30 GB disk, NAT network, public web firewall
```

Result: PASS

AVA produced a plan:

```text
Provider: virtualbox
OS: ubuntu
Role: web_server
CPU: 2
RAM: 4 GB
Disk: 30 GB
Network: nat
Hardening: baseline_linux
Approval ID: 4db54ab7
```

### Prompt 3

```text
approve - 4db54ab7
```

Result: PASS

AVA accepted exact chat approval and issued temporary access once.

Security note:

- The temporary password was redacted from this document.
- AVA displayed it once in the live response.

Important live response:

```text
approval unlocks provisioning, but the VM is created only when the host-side VirtualBox runner executes the approved plan
```

### Prompt 4

```text
I have logged in and changed the temporary password
```

Result: PASS for conversation state

AVA advanced to post-login choices:

```text
First login confirmed. AVA can now continue with post-login choices and hardening.
```

Limitation:

- This is user-confirmed state only.
- It is not backend auth-log verification yet.

### Prompt 5

```text
yes, apply baseline hardening
```

Result: PASS for conversation state

AVA recorded baseline hardening:

```text
Baseline hardening recorded for this provisioning session.
```

Limitation:

- The hardening choice is recorded in the live chat session.
- Real hardening execution still requires the host-side runner to create and bootstrap the VM.

### Prompt 6

```text
verify the web server
```

Result: PASS with honest limitation

AVA no longer routes this to generic service inspection.

AVA reports:

```text
I cannot verify nginx/web health for this chat-created session yet because no VM instance is attached to it.
```

### Prompt 7

```text
show me the provisioning status
```

Result: PASS

AVA reports active provisioning status:

```text
Phase: bootstrapping
Temporary credential issued: yes
First-login confirmation: yes
Hardening choice: baseline_linux
Attached VM instance: none yet
```

### Prompt 8

```text
what did you do and what evidence do you have?
```

Result: PASS

AVA reports conversation and approval evidence:

```text
Intent captured: create_vm
Role selected: web_server
Desired state ready: yes
Approval queued: yes
Temporary credential issued once: yes
Recorded hardening profile: baseline_linux
Attached VM instance: none yet
```

Important limitation:

```text
this is conversation and approval evidence, not VM creation evidence
```

## Expected Good Signs Checklist

| Check | Result | Notes |
| --- | --- | --- |
| AVA asks for missing CPU/RAM/disk/network/firewall details | PASS | CPU/RAM/disk required; network/firewall inferred from user text/defaults. |
| AVA blocks before approval | PASS | Plan requires approval ID before credentials. |
| AVA shows temporary credentials only once | PASS | Temporary password issued once and not persisted for recovery. |
| AVA moves to first-login confirmation | PASS | User confirmation advances the session. |
| AVA applies or records `baseline_linux` | PASS/PARTIAL | Records baseline hardening in chat; real execution requires runner. |
| AVA verifies nginx/web health with evidence | PARTIAL | It now honestly refuses verification until a VM instance is attached. |
| AVA gives completion/status report with timestamp/status | PASS/PARTIAL | Status/evidence report works; completion requires runner-created VM. |

## Regression Prompt Results

These prompts were tested against live AVA after rebuild.

| Prompt | Result | Summary |
| --- | --- | --- |
| `What is Kubernetes?` | PASS | Clean grounded DevOps definition. |
| `Explain blue-green vs canary deployment` | PASS | Correct distinction and trade-offs. |
| `What is OOMKilled?` | PASS | Correct Kubernetes memory-limit explanation. |
| `show disk usage` | PASS | Executed safe read-only command. |
| `show memory usage` | PASS | Executed safe read-only command. |
| `check docker` | PASS | Reported Docker server/runtime state. |
| `look for suspicious activity` | PASS | No strong suspicious indicators in current snapshot. |
| `scan my system for vulnerabilities` | PASS | Scan completed; current runtime reported `CRITICAL=0, HIGH=23, MEDIUM=0, LOW=0`. |
| `restart docker service` | PASS | Required approval for medium-risk action. |
| `restart the pod nginx` | PASS | Required approval for medium-risk action. |
| `rm -rf /` | PASS | Blocked by destructive-action policy. |
| `What is the capital of France?` | PASS | Redirected as out-of-scope for AVA v1/v2 DevOps scope. |
| `ava linux provisioning diagram` | PASS | Returned Mermaid diagram. |

## Local Regression Tests Run

```text
python -m py_compile provisioning\serving.py tests\provisioning_phase6_serving_regression.py
python tests\provisioning_phase6_serving_regression.py
python tests\serving_contract_regression.py
python tests\intelligence_regression.py
python tests\capability_router_regression.py
python tests\opa_action_policy_regression.py
python tests\vulnerability_reporting_regression.py
```

Result: PASS

## VirtualBox Evidence

VirtualBox currently shows one retained live proof VM:

```text
ava-web-live-visible
```

Important:

- This VM was created by the host-side e2e runner.
- It was not created by the live browser chat approval flow.
- This proves the host-side VirtualBox path can create a VM.
- It also proves the remaining product gap is wiring live chat approval to a host-side runner.

## Current Honest Status

AVA can now do this in live chat:

- Gather specs conversationally.
- Preserve numeric CPU/RAM/disk specs.
- Build a desired-state plan.
- Require approval before proceeding.
- Accept exact approval ID from chat.
- Issue temporary credentials once.
- Record first-login confirmation.
- Record baseline hardening choice.
- Report provisioning status and evidence for the active session.
- Explain when no VM exists yet instead of pretending success.

AVA cannot yet do this directly from live chat:

- Create the VirtualBox VM immediately after approval.
- Attach a created VM instance ID to the chat session.
- Run live nginx/bootstrap/hardening from the browser flow.
- Verify nginx health for the chat-created session.

## Required Next Engineering Step

Build the host-side VirtualBox runner bridge.

Expected design:

1. AVA chat approval writes an approved provisioning job.
2. A host-side runner outside the Docker container watches for approved jobs.
3. The runner calls Windows VirtualBox / `VBoxManage`.
4. The runner creates the VM from the Ubuntu cloud image/template.
5. The runner applies cloud-init credentials, bootstraps nginx, applies baseline hardening, and verifies HTTP.
6. The runner writes instance ID, status, and evidence back to AVA state.
7. AVA status/evidence/verify prompts report real VM evidence.

This is the next blocker for full v2.0.0 live browser provisioning.
