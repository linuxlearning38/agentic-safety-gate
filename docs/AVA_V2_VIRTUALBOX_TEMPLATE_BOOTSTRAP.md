# AVA v2 VirtualBox Template Bootstrap

Date: 2026-04-30  
Branch: `v2-development`  
Status: In progress

## Purpose

This document explains from scratch how AVA v2 is being bootstrapped for the
first provider slice:

- provider: `VirtualBox`
- OS: `Ubuntu`
- role: `web_server`

The goal is to end with a reusable local base VM named `ubuntu-cloud-image`
that AVA can clone repeatedly during `v2.0.0`.

This document is intentionally practical. It records:

- what was attempted
- what worked
- what failed
- what code was created
- why the current approach was chosen

## Why A Base Template Exists

AVA v2 is **not** provisioning from a full ISO installer every time.

The locked `v2.0.0` design uses:

- one reusable Ubuntu base template
- one provider adapter that clones that template
- one role bootstrap layer that configures the clone

That keeps later provisioning runs fast and repeatable.

So the one-time bootstrap problem is:

1. create or install `ubuntu-cloud-image`
2. verify it boots correctly
3. keep it as the source VM that AVA clones later

## What Was Tried First

### 1. Direct ISO bootstrap helper

File:
- [C:\Users\mmc\Documents\New project 3\devops-agent\scripts\prepare_virtualbox_ubuntu_template.ps1](C:/Users/mmc/Documents/New%20project%203/devops-agent/scripts/prepare_virtualbox_ubuntu_template.ps1)

This script was extended to support:

- direct source disk cloning
- ISO-based bootstrap VM creation

With the ISO path:

- `F:\Softwares\ubuntu-22.04.5-live-server-amd64.iso`

it successfully created:

- VM: `ubuntu-cloud-image`
- disk: `I:\ai-lab\virtualbox-vms\ubuntu-cloud-image\ubuntu-cloud-image.vdi`

### 2. VirtualBox unattended install

VirtualBox `unattended install` was tested against the Ubuntu Server ISO.

It did start, and it generated:

- a VISO overlay file
- a generated `grub.cfg`
- a generated `preseed.cfg`
- a generated post-install script

But it still dropped into interactive installer screens instead of completing
zero-click.

Observed interactive screens included:

- language selection
- installer update prompt
- keyboard configuration
- storage configuration

That means the built-in VirtualBox unattended path is **not reliable enough**
for AVA’s repeatable bootstrap story on this host/ISO combination.

## Why The Approach Changed

The problem is not VirtualBox VM creation.

The problem is the installer automation layer.

We need a bootstrap path that is:

- reproducible
- code-driven
- local-only
- not dependent on manual key presses

Since this machine does **not** currently have:

- `cloud-localds`
- `genisoimage`
- `mkisofs`
- `xorriso`
- `oscdimg`

the solution cannot depend on external ISO-building tools.

## Current Chosen Approach

Use a **VirtualBox VISO overlay** plus Ubuntu **autoinstall** data.

This works because VirtualBox already proved locally that it can mount a VISO
file that:

- imports the original Ubuntu Server ISO
- overrides boot files such as `grub.cfg`
- injects extra files into the mounted media

So instead of using VirtualBox preseed unattended mode, AVA now prepares:

1. a custom `grub.cfg`
2. a `user-data` file
3. a `meta-data` file
4. a `.viso` file that overlays those onto the original ISO

Then the VM boots from that VISO-backed install media.

## Files Created For The New Autoinstall Path

### Main script

- [C:\Users\mmc\Documents\New project 3\devops-agent\scripts\prepare_virtualbox_ubuntu_template_autoinstall.ps1](C:/Users/mmc/Documents/New%20project%203/devops-agent/scripts/prepare_virtualbox_ubuntu_template_autoinstall.ps1)

This script creates:

- a new `ubuntu-cloud-image` VM
- a target VDI disk
- a custom autoinstall `grub.cfg`
- `nocloud` `user-data`
- `nocloud` `meta-data`
- a VirtualBox `.viso` overlay file
- a headless unattended start, when requested

### Adapter and test files already prepared

- [C:\Users\mmc\Documents\New project 3\devops-agent\provisioning\adapters\virtualbox.py](C:/Users/mmc/Documents/New%20project%203/devops-agent/provisioning/adapters/virtualbox.py)
- [C:\Users\mmc\Documents\New project 3\devops-agent\tests\virtualbox_adapter_smoke.py](C:/Users/mmc/Documents/New%20project%203/devops-agent/tests/virtualbox_adapter_smoke.py)
- [C:\Users\mmc\Documents\New project 3\devops-agent\tests\virtualbox_adapter_live_smoke.py](C:/Users/mmc/Documents/New%20project%203/devops-agent/tests/virtualbox_adapter_live_smoke.py)

## Bootstrap Credentials Used During Base Template Install

For the bootstrap template only:

- username: `ubuntu`
- password: `ubuntu`

Why:

- the official Ubuntu autoinstall quickstart provides a known working example
  hash for this password
- it avoids needing external password-hash tooling on the Windows host
- this is only for the one-time local base template install

This is **not** the final AVA product credential story.

Later `v2` phases will handle:

- temporary access issuance
- one-time display
- forced password change
- operator confirmation in AVA

## What “Template Ready” Means

The template is only considered ready when all of these are true:

1. `ubuntu-cloud-image` installs successfully
2. the VM powers off or reboots cleanly after install
3. it can boot from disk without the install flow
4. AVA’s live smoke can inspect it successfully
5. AVA can clone it and configure NAT SSH/HTTP forwarding

Only after that can we say Phase 1 is truly closed.

## What Has Already Been Verified

Verified:

- VirtualBox exists on the Windows host
- the base VM can be created from code
- the base disk can be created from code
- the Ubuntu Server ISO at `F:\Softwares\ubuntu-22.04.5-live-server-amd64.iso`
  is valid
- AVA’s VirtualBox adapter command flow is tested in mocked smoke
- live bootstrap VM shell creation works
- VirtualBox VISO overlays are supported locally

Not yet fully verified:

- custom autoinstall VISO path end to end
- final installed template boot
- post-install live adapter smoke

## Practical Sequence

The intended clean sequence from here is:

1. stop/remove the current partially interactive bootstrap run
2. generate the autoinstall overlay files
3. recreate `ubuntu-cloud-image` with the autoinstall VISO
4. start the VM headless
5. wait for the install to complete
6. verify the machine boots from disk
7. run the live adapter smoke

## Why This Matters For Later AVA UX

The user-facing AVA flow later will be:

- “I want a web server in Ubuntu”
- AVA collects specs
- AVA provisions a VM quickly
- AVA gives access
- AVA continues with role bootstrap and hardening

That experience only feels fast if the provider layer clones a prepared base
template instead of reinstalling Ubuntu every time.

So this bootstrap work is not a side task. It is the foundation for the whole
VirtualBox provider slice.
