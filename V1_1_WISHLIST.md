# AVA v1.1 Wishlist

Date: 2026-04-28
Status: Sequenced polish-first backlog

## Intent

`v1.1` should improve the experience of the existing AVA product without
quietly expanding `master` into provisioning, orchestration, or unattended
automation.

This is a polish-first milestone, not a scope jump.

---

## Sequence

### Step 1 — Truth Alignment

Keep repo-facing and user-facing documentation aligned with the shipped runtime:

- README always reflects the latest stable line
- `AGENTS.md` stays current on serving-contract truth
- capability documentation points to one maintained source of truth
- release notes stay complete for every shipped tag

### Step 2 — Operator UX Polish

Improve the feel of the current product without adding new trust surfaces:

- cleaner wording in security and host-risk summaries
- clearer vulnerability/remediation explanations
- better follow-up phrasing where AVA already has the right answer
- more consistent diagram readability and labels
- fewer internal implementation terms leaking into responses

### Step 3 — Demo And Public Presentation

Make the project easier to understand and share:

- tighten README positioning and examples
- make capability references easier to find
- keep validation notes easy to discover
- prepare a concise demo flow for common AVA strengths

### Step 4 — Small Safe Improvements Only If Needed

Only after the polish work is stable:

- better summarization of safe inspection results
- better multi-turn continuation for existing operational flows
- richer but still bounded read-only inspection outputs

No new provisioning, cloud orchestration, or autonomous healing scope belongs in
`v1.1`.

---

## Current High-Value v1.1 Tasks

1. Keep public docs aligned with the latest shipped tag.
2. Remove duplicated or stale documentation truth where it causes confusion.
3. Improve AVA response wording where outputs are technically correct but still feel rough.
4. Keep diagram answers readable and visually consistent.
5. Preserve and extend the regression baseline around serving-contract behavior.

---

## Explicit Non-Goals

These are not `v1.1` work:

- VM provisioning execution on `master`
- AWS, Azure, GCP, or VMware orchestration
- unattended hardening or healing loops
- replacing AVA routing with raw LLM behavior
- turning v1 into a multi-host control plane

---

## Release Standard

`v1.1` should only ship when:

- repo-facing documentation matches the current runtime truth
- key user-facing answers feel clean and intentional
- regression coverage remains green
- no new scope confusion is introduced on `master`
