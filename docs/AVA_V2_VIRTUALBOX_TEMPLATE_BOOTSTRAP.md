# AVA v2 VirtualBox Template Bootstrap

Date: 2026-04-30  
Branch: `v2-development`  
Status: Cloud-image OVA and per-clone cloud-init access verified

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

Use the official Canonical Ubuntu cloud-image OVA as the preferred template
source.

The ISO autoinstall path remains documented because it was useful while
learning the host behavior, but it is no longer the preferred path for Phase 1.

Why the cloud-image OVA is preferred:

- it is already a cloud-ready Ubuntu image
- it avoids a long interactive installer path
- it matches the locked "cloud image / template" v2 decision
- it is easier to import, clone, and test repeatedly

Official image source:

- `https://cloud-images.ubuntu.com/releases/jammy/release/ubuntu-22.04-server-cloudimg-amd64.ova`

## Cloud-Init Access Mechanism

The imported OVA is only the base image. Each clone still needs per-VM
customization for:

- hostname
- username
- temporary password
- SSH access material
- first-boot proof marker

AVA now uses cloud-init's NoCloud local seed-media pattern for that.

The seed mechanism is:

1. create a temporary seed directory
2. write `user-data`
3. write `meta-data`
4. build a small ISO with volume label `CIDATA`
5. attach that ISO to the cloned VM before first boot
6. let cloud-init customize the clone on first boot

Why this approach:

- cloud-init officially supports labelled ISO/VFAT seed media named `CIDATA`
- it works without a metadata server
- it works offline
- it keeps the base OVA generic
- each clone gets unique metadata

The Windows host does not need `cloud-localds`, `genisoimage`, `mkisofs`,
`xorriso`, or `oscdimg`. AVA creates the ISO with Windows IMAPI2FS through:

- [C:\Users\mmc\Documents\New project 3\devops-agent\scripts\new_cloud_init_seed_iso.ps1](C:/Users/mmc/Documents/New%20project%203/devops-agent/scripts/new_cloud_init_seed_iso.ps1)

The VirtualBox adapter then attaches that ISO with:

- `VirtualBoxAdapter.inject_access(...)`

For the live smoke, AVA seeds both:

- the intended product-style temporary username/password
- a test-only SSH public key so the smoke can verify access without manual
  password typing

The SSH key is for automation only. The v2 product UX remains temporary
username/password with a forced password-change workflow later in the
conversation layer.

## Previous Autoinstall Approach

The explored fallback path used a **VirtualBox VISO overlay** plus Ubuntu
**autoinstall** data.

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

The VISO path successfully reached real `subiquity` autoinstall execution, but
the host then hit installer/runtime fragility around snapd seeding and late
installer behavior. That made it less suitable as the primary v2 template path.

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

### Finalization script

- [C:\Users\mmc\Documents\New project 3\devops-agent\scripts\finalize_virtualbox_ubuntu_template.ps1](C:/Users/mmc/Documents/New%20project%203/devops-agent/scripts/finalize_virtualbox_ubuntu_template.ps1)

This script is meant to run **after** the autoinstall VM has finished and
powered off. It:

- verifies the template VM is no longer running
- switches boot order to disk-only
- detaches the installer media from SATA port 1
- marks the template as `ready` through VirtualBox extradata

### Preferred cloud-image import script

- [C:\Users\mmc\Documents\New project 3\devops-agent\scripts\prepare_virtualbox_ubuntu_cloud_ova_template.ps1](C:/Users/mmc/Documents/New%20project%203/devops-agent/scripts/prepare_virtualbox_ubuntu_cloud_ova_template.ps1)

This script:

- downloads the official Ubuntu 22.04 cloud-image OVA if it is not already
  cached
- imports it into VirtualBox
- renames it to `ubuntu-cloud-image`
- configures NAT networking
- sets disk-only boot order
- marks the VM as `AVA:template=ready`

### Adapter and test files already prepared

- [C:\Users\mmc\Documents\New project 3\devops-agent\provisioning\adapters\virtualbox.py](C:/Users/mmc/Documents/New%20project%203/devops-agent/provisioning/adapters/virtualbox.py)
- [C:\Users\mmc\Documents\New project 3\devops-agent\tests\virtualbox_adapter_smoke.py](C:/Users/mmc/Documents/New%20project%203/devops-agent/tests/virtualbox_adapter_smoke.py)
- [C:\Users\mmc\Documents\New project 3\devops-agent\tests\virtualbox_adapter_live_smoke.py](C:/Users/mmc/Documents/New%20project%203/devops-agent/tests/virtualbox_adapter_live_smoke.py)
- [C:\Users\mmc\Documents\New project 3\devops-agent\tests\virtualbox_cloud_init_access_smoke.py](C:/Users/mmc/Documents/New%20project%203/devops-agent/tests/virtualbox_cloud_init_access_smoke.py)

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
6. AVA can attach clone-specific cloud-init seed media
7. AVA can boot the clone and verify SSH access through the seeded user

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
- official Canonical Ubuntu 22.04 cloud-image OVA download
- VirtualBox import as `ubuntu-cloud-image`
- template marked as `AVA:template=ready`
- live adapter smoke clone
- NAT SSH forwarding metadata
- NAT HTTP forwarding metadata
- cleanup of the smoke clone
- Windows-native `CIDATA` cloud-init seed ISO creation
- first-boot customization of a cloned cloud-init guest
- seed ISO attachment through `VirtualBoxAdapter.inject_access`
- SSH TCP reachability through NAT forwarding
- SSH command execution through the seeded cloud-init user
- cloud-init marker verification inside the started clone
- cleanup of the cloud-init access smoke clone

Not yet fully verified:

- web role bootstrap on a clone

## First Real Autoinstall Failure Found

During the first full autoinstall run, the installer reached late-stage
configuration and then failed on one of AVA’s own late commands.

The failing command was the earlier attempt to modify:

- `/etc/ssh/sshd_config`

with an inline `sed -i ...` expression through `curtin in-target`.

That failed because the quoting and regex were too brittle for the installer’s
execution environment.

### Fix applied

Instead of editing the main SSH config file inline, the bootstrap script now
creates a dedicated SSH drop-in file:

- `/etc/ssh/sshd_config.d/99-ava-password-auth.conf`

with:

- `PasswordAuthentication yes`

Why this is better:

- less quoting complexity
- safer than rewriting the base config
- easier to audit
- easier to remove later if AVA moves to key-only bootstrap modes

## Practical Sequence

The intended clean sequence from here is:

1. remove any partial `ubuntu-cloud-image` installer VM
2. import the official Ubuntu cloud-image OVA
3. mark the imported VM as `AVA:template=ready`
4. run the live adapter smoke
5. use the imported template as the source for AVA clones

## Verified Cloud-Image Run

The preferred cloud-image import and clone-access path was verified on
2026-04-30.

Source:

- `https://cloud-images.ubuntu.com/releases/jammy/release/ubuntu-22.04-server-cloudimg-amd64.ova`

Local cache:

- `I:\ai-lab\downloads\ubuntu-22.04-server-cloudimg-amd64.ova`

Imported template:

- `ubuntu-cloud-image`

Live smoke result:

- template clone succeeded
- clone registered successfully
- SSH host forwarding exposed `127.0.0.1:2222`
- HTTP host forwarding exposed `127.0.0.1:8080`
- smoke VM was destroyed after the check

Cloud-init access smoke result:

- NoCloud seed ISO created with `CIDATA` volume label
- clone-specific `user-data` and `meta-data` attached before first boot
- clone started headlessly
- SSH became reachable through `127.0.0.1:2222`
- seeded user executed an SSH command successfully
- `/var/tmp/ava-cloud-init-ready` marker was verified
- smoke VM was destroyed after the check

## Finalization Rule

The template is not considered clone-ready just because the install ran.

It becomes clone-ready only after:

- the VM powers off cleanly
- installer media is detached
- boot order is disk-first only
- the template is marked `AVA:template=ready`
- the live smoke can inspect it successfully

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
