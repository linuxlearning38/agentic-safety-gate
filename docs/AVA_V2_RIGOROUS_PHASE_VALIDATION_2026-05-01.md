# AVA v2 Rigorous Phase Validation - 2026-05-01

Documented: `2026-05-02 00:12:34 +05:30`
Timezone: `Asia/Calcutta (+05:30)`
Branch: `v2-development`
Final validation commit: `b8421b7 test(v2): harden cloud-init access smoke wait`

## Summary

A rigorous v2 validation pass was completed across the locked VirtualBox +
Ubuntu + web server slice.

Result: `PASS after one test hardening fix`

The suite covered:

- compile checks for v2 provisioning modules
- Phase 2 through Phase 7 regression tests
- broader AVA serving and security guardrails
- live VirtualBox adapter smoke
- live cloud-init access smoke
- live nginx web-server bootstrap smoke
- full v2 end-to-end release gate

The only issue found was in a legacy smoke test timeout, not in the product
flow itself. The cloud-init access smoke used a 20-second SSH command timeout,
which was too short during first boot. The test was hardened to allow a
60-second command timeout and to retry safely after timeout. The smoke then
passed, and the full e2e release gate passed afterward.

## Test Environment

- Repository: `C:\Users\mmc\Documents\New project 3\devops-agent`
- Provider under test: `VirtualBox`
- OS/template under test: Ubuntu cloud image template
- Role under test: `web_server`
- Network mode: NAT with host forwarding
- SSH forwarding: `127.0.0.1:2222`
- HTTP forwarding: `127.0.0.1:8080`
- Locked v2.0.0 slice: VirtualBox + Ubuntu + web server only

## Phase Coverage Map

- Phase 1 template/provider readiness: covered by VirtualBox adapter smoke,
  cloud-init access smoke, and web-server bootstrap smoke.
- Phase 2 desired-state/session handling: covered by
  `provisioning_phase2_state_regression.py`.
- Phase 3 policy and credential flow: covered by
  `provisioning_phase3_policy_credentials_regression.py`.
- Phase 4 SSH bootstrap and role application: covered by
  `provisioning_phase4_role_bootstrap_regression.py` and live web bootstrap.
- Phase 5 verification and state recording: covered by
  `provisioning_phase5_verification_state_regression.py`, live web bootstrap,
  and full e2e.
- Phase 6 serving integration: covered by
  `provisioning_phase6_serving_regression.py`.
- Phase 7 rollback reporting: covered by
  `provisioning_phase7_rollback_regression.py` and full e2e cleanup behavior.
- Phase 8 release gate: covered by `tests/v2_e2e_test.py`.

Phase 0 architecture/contract work is represented by the committed design
documents and branch state. It is not a runtime test phase.

## Compile Check

Command:

```powershell
python -m py_compile control\input_router.py provisioning\desired_state.py provisioning\policy.py provisioning\credentials.py provisioning\serving.py provisioning\rollback.py provisioning\verify\engine.py provisioning\state\store.py provisioning\adapters\virtualbox.py provisioning\bootstrap\ssh_executor.py provisioning\roles\web_server.py tests\v2_e2e_test.py
```

Result: `PASS`

## Phase Regression Tests

All phase regression tests passed:

```powershell
python tests\provisioning_phase2_state_regression.py
python tests\provisioning_phase3_policy_credentials_regression.py
python tests\provisioning_phase4_role_bootstrap_regression.py
python tests\provisioning_phase5_verification_state_regression.py
python tests\provisioning_phase6_serving_regression.py
python tests\provisioning_phase7_rollback_regression.py
```

Results:

- Phase 2 state/session regression: `PASS`
- Phase 3 policy/credential regression: `PASS`
- Phase 4 role bootstrap regression: `PASS`
- Phase 5 verification/state regression: `PASS`
- Phase 6 serving regression: `PASS`
- Phase 7 rollback regression: `PASS`

## Broader AVA Guardrail Tests

The v2 pass also reran broader AVA safety and serving checks:

```powershell
python tests\serving_contract_regression.py
python tests\intelligence_regression.py
python tests\security_hardening_regression.py
python tests\opa_action_policy_regression.py
python tests\capability_router_regression.py
```

Results:

- Serving contract regression: `PASS`
- Intelligence regression: `PASS`
- Security hardening regression: `PASS`
- OPA action policy regression: `PASS`
- Capability router regression: `PASS`

## Live VirtualBox Tests

### VirtualBox Adapter Smoke

Command:

```powershell
python tests\virtualbox_adapter_smoke.py
```

Result: `PASS`

Coverage:

- VirtualBox adapter can create a real clone
- VM lifecycle operations complete
- cleanup succeeds

### Cloud-Init Access Smoke

Command:

```powershell
python tests\virtualbox_cloud_init_access_smoke.py
```

Initial result: `FAIL`

Observed behavior:

- VM clone was created
- cloud-init access media was attached
- VM started
- SSH TCP became reachable
- the single cloud-init marker command timed out after 20 seconds
- cleanup destroyed VM `ava-cloudinit-smoke-20260501182944`

Fix:

- Hardened `_wait_for_ssh_command` in
  `tests\virtualbox_cloud_init_access_smoke.py`
- Increased per-command timeout from 20 seconds to 60 seconds
- Converted timeout into a retryable failed attempt instead of a hard crash

Commit:

```text
b8421b7 test(v2): harden cloud-init access smoke wait
```

Rerun result: `PASS`

Observed successful rerun:

- VM: `ava-cloudinit-smoke-20260501183118`
- SSH: `avaadmin@127.0.0.1:2222`
- cloud-init marker verified
- cleanup destroyed the VM

### Web Server Bootstrap Smoke

Command:

```powershell
python tests\virtualbox_web_server_bootstrap_smoke.py
```

Result: `PASS`

Observed successful run:

- VM: `ava-web-smoke-20260501183253`
- SSH reachable through `127.0.0.1:2222`
- cloud-init marker verified
- `apt-get update` completed
- nginx and ufw installed
- UFW allowed ports `22/tcp` and `80/tcp`
- nginx enabled and active
- host HTTP returned `HTTP 200` at `http://127.0.0.1:8080/`
- verification engine passed
- state was persisted
- cleanup destroyed the VM

## Full v2 End-to-End Release Gate

Command:

```powershell
python tests\v2_e2e_test.py
```

Result: `PASS`

Observed successful run:

- Session: `ead30b53-47e1-4fb9-bb60-7dbcf9a3c5f3`
- VM: `ava-v2-e2e-20260501183823`
- Approval queued: `cac37b72`
- Temporary credential issued after approval for user `avaadmin`
- Cloud-init seed ISO created
- Instance created and started
- SSH TCP reachable through `127.0.0.1:2222`
- Cloud-init marker confirmed
- First-login confirmation accepted by guided flow
- Default hardening choice recorded as `baseline_linux`
- nginx and ufw bootstrapped successfully
- Host HTTP returned `HTTP 200`
- Verification engine passed
- State store recorded completion
- Wall time: `142.5s`
- Release gate limit: `600s`
- Cleanup destroyed the VM

The e2e release gate verified the full locked v2.0.0 flow:

1. User intent starts guided provisioning.
2. AVA asks for missing specs.
3. AVA queues approval.
4. Pending approval blocks credential issuance.
5. Approval releases one-time temporary credential.
6. Cloud-init seed is generated.
7. VirtualBox clone is created.
8. Access media is attached.
9. VM starts.
10. First access is confirmed.
11. Hardening choice is recorded.
12. Web server role is bootstrapped.
13. HTTP 200 is verified.
14. Verification evidence is persisted.
15. VM is cleaned up.

## Files Changed By This Validation

Only one tracked file changed during the validation:

- `tests\virtualbox_cloud_init_access_smoke.py`

Reason:

- The smoke test needed a more realistic first-boot SSH/cloud-init wait.

Untracked `.claude/` remained untouched.

## Final Readout

Final rigorous status: `PASS`

AVA v2's locked first slice is holding across:

- guided provisioning state
- approval gating
- one-time credential handling
- VirtualBox clone lifecycle
- cloud-init access injection
- SSH bootstrap
- baseline Linux hardening choice
- nginx web role installation
- HTTP verification
- state persistence
- rollback/cleanup behavior
- broader AVA security and serving guardrails

This validation does not claim that future providers or roles are complete.
It confirms that the current v2.0.0 VirtualBox + Ubuntu + web server slice
passed a rigorous local verification run after the test timeout hardening fix.
