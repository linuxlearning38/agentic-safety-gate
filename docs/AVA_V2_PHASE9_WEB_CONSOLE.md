# AVA v2 Phase 9.7 - AVA Web Console

Branch: `v2-development`
Status: Phase 9.7 completion slice
Target version: `v2.1.x`

## Purpose

AVA Web Console is the product-safe replacement for depending on PuTTY on the
user's machine.

The goal is simple: when AVA is reachable from a browser, the user should be
able to open a protected SSH console to an AVA-managed VM from inside AVA
itself.

This is not raw public SSH exposure. AVA remains the gateway.

## Product Contract

AVA Web Console must:

- work from any browser that can reach the AVA UI
- require the existing AVA login/JWT session
- target only AVA-created and AVA-managed VMs in this first slice
- use the Windows host runner as the SSH bridge
- keep SSH private keys on the host runner, never in the browser
- use the retained `ava-runner` key instead of passing VM passwords
- audit and report console state through AVA-controlled runtime state
- fail closed when runner, VM, session, or key evidence is missing

## Architecture

```text
Browser
  -> AVA UI over HTTPS
  -> /console/* endpoints with JWT auth
  -> Redis console session/input/output channels
  -> Windows host runner
  -> OpenSSH using retained ava-runner key
  -> AVA-managed VirtualBox Ubuntu VM
```

## Phase 9.7 Scope

Implemented:

- `Web Console` button in the AVA sidebar.
- Chat shortcut for `open console`, `open web console`, and `open ssh console`.
- `/console/open` creates a console request for the latest AVA-managed VM.
- `/console/<id>/output` streams console output to the browser by polling.
- `/console/<id>/input` sends browser input to the runner.
- `/console/<id>/close` requests session shutdown.
- Host runner consumes console requests without blocking provisioning jobs.
- Host runner SSHs as `ava-runner` using the retained runner key.
- Console sessions are scoped to the authenticated AVA user.
- Each console session uses a scoped `known_hosts` file under
  `.ava-runner/known_hosts_console_sessions/` so recreated local NAT VMs do not
  collide with stale SSH host fingerprints or the legacy flat
  `.ava-runner/known_hosts_console` file.
- Console polling has a dedicated authenticated rate limit and client-side
  backoff, so AVA does not throttle its own browser console stream.
- New console sessions start a quiet non-login shell with `PAGER=cat`,
  `SYSTEMD_PAGER=cat`, and a plain prompt. This avoids the Ubuntu MOTD banner
  and keeps common commands like `systemctl status nginx` from trapping the
  simple browser console in a pager.
- The console includes an `Interrupt` button that sends Ctrl+C to the active
  SSH session.
- The host runner reads SSH output as UTF-8 with replacement instead of using
  the Windows default code page. Terminal bytes must never crash the console
  reader thread.
- The first text-panel implementation strips terminal escape/control sequences
  with a stateful browser-side parser before rendering, even when SSH output
  arrives one byte at a time. A later `xterm.js` slice should render those
  sequences natively instead of stripping them.
- The UI labels the console as `Basic mode` so users understand this is a
  stable browser shell bridge, not the final PuTTY-speed terminal.
- The UI shows live connection state: `Idle`, `Opening`, `Queued`,
  `Connected`, `Busy`, `Failed`, and `Closed`.
- Reconnect closes the previous console session before opening a replacement.
- Repeated adjacent shell prompts are normalized so normal command output stays
  readable.

This first slice uses HTTP polling, not WebSockets. That keeps the feature
inside the current Flask/Gunicorn stack. A later slice can replace polling with
`xterm.js` plus WebSocket transport while keeping the same security model.

## Acceptance Checklist

Phase 9.7 is considered complete when:

- AVA can provision an Ubuntu web server VM through the existing approval flow.
- `verify the web server` returns live host-runner evidence.
- `open web console` opens the in-browser console for the latest AVA-managed VM.
- The console connects as `ava-runner` without requiring PuTTY on the browser
  machine.
- Basic commands work from the browser console:
  - `whoami`
  - `hostname`
  - `curl -I http://127.0.0.1`
  - `systemctl --no-pager status nginx`
- The console does not expose the private runner key or VM password to the
  browser.
- Reconnect opens a fresh session and does not keep appending to a stale one.
- Close requests the host runner to terminate the console session.
- The UI clearly says this is `Basic mode`.
- The regression suite passes:
  - `python tests\provisioning_phase9_runner_bridge_regression.py`
  - `python tests\provisioning_phase6_serving_regression.py`

## Known Limits

These are accepted limits for Phase 9.7 and are intentionally deferred:

- The console is polling-based, so it is slower than PuTTY.
- It is suitable for ordinary commands and troubleshooting, not full-screen TUI
  programs such as `vim`, `top`, `less`, or interactive installers.
- Arrow-key history, tab completion rendering, terminal resize, and perfect ANSI
  handling require the WebSocket/xterm phase.
- The first slice targets the latest AVA-managed VM only. A VM picker is a
  future slice.
- Manual public SSH targets are not enabled in this phase.

## Security Boundary

Allowed in Slice 1:

- browser console to the latest AVA-managed VM attached to the user's AVA
  provisioning history
- key-based SSH as `ava-runner`
- command input typed by the authenticated user

Blocked or deferred:

- arbitrary public IP SSH targets
- storing or replaying VM passwords
- sending private keys to the browser
- unauthenticated console access
- exposing VM NAT SSH ports publicly
- direct browser-to-VM SSH

## Manual SSH Console Roadmap

The PuTTY-like manual flow is intentionally separate from Slice 1.

Target UX:

1. User opens `AVA Web Console`.
2. User chooses either `AVA-managed server` or `Manual SSH target`.
3. Manual mode asks for host/IP, port, username, and auth method.
4. Passwords are used only for the live session and are never persisted.
5. Public IP targets require approval and an allowlist policy.
6. AVA shows SSH host-key warnings instead of silently trusting changed hosts.

Product rule: do not expose raw VM SSH ports publicly just to make the console
work. AVA should remain the authenticated gateway.

## Public Access Model

When AVA is later exposed through a private/public URL, expose AVA only:

- Tailscale / private VPN access, or
- Cloudflare Tunnel / reverse proxy with HTTPS, or
- a hardened TLS reverse proxy with AVA auth enabled

Do not expose the VM's SSH port directly to the internet. The browser should
talk to AVA, and AVA should broker the console through the host runner.

## Next Slices

1. Replace the simple text panel with `xterm.js`.
2. Add terminal resize support.
3. Add session audit records for open/close and duration.
4. Add an explicit VM picker for multiple AVA-managed VMs.
5. Add optional approved external SSH targets with allowlist and audit.
