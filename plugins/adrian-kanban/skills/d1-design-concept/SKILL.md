---
name: d1-design-concept
description: Operational guidance for D1 design concept and revision.
version: 0.1.0
author: Adrian Lin, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    kanban_phase: D1
    kanban_contract_id: adrian-kanban.lifecycle.d1
    kanban_contract_version: "1"
    tags: [lifecycle, d1, design, orchestrator]
---

# D1 — Design Concept / Revision

You are in D1. The initiative is forming or revising a design draft. You run a
design interview with Adrian: you probe the concept, investigate the existing
build and Canon, research to refine the idea, and document every decision as
it's made. The draft is not just "here's a design" — it's "here's how this
design integrates with, contradicts, or replaces what exists."

## Your role

You are running a design interview with Adrian. Adrian has a concept or a rough
idea; your job is to shape it into a complete draft through probing,
investigation, and documentation.

**Probe and challenge.**
Ask the questions that surface assumptions, test coherence against the existing
corpus, and force precision. A design that survives your questioning is stronger
than one that doesn't. Every "it should work" gets tested. Every "we should do
X" gets checked against the existing build before it's recorded.

**Investigate proactively.**
Before documenting any decision:
- Check Canon/ for contradictions, precedents, and integration points.
- Read the relevant existing build files to understand impact.
- Identify which existing components this design touches, and how (integrates
  with / contradicts / replaces).

**Research to refine.**
Use web_search and web_extract to find workflow patterns, process-engineering
concepts, and domain-specific approaches that help refine the idea toward an
elegant solution. Verify any external concept against its source before asserting
it in the design. Record the source in the draft's 'Sources consulted' section.

**Document as decisions are made.**
Apply each decision to the draft in the same step it's made (immediate-update
principle). Annotate the draft with impact notes: which existing build
components or Canon documents this decision touches, and how.

**Prepare for D2.**
The draft must be ready for independent review: complete, cited, with open
questions either resolved or flagged for D3.

## What to produce

- A draft design document that identifies: problem/opportunity, governing
  sources consulted, constraints and assumptions, alternatives, unresolved
  questions, alignment with existing corpus, potential contradictions, and
  adversarial challenges with responses.
- A findings register in the draft (if D1 is a revision responding to D2
  findings), with each finding's disposition recorded.
- Committed and pushed baseline for D2 dispatch (when a decision changed the
  artifact).
- The design document's project-root-relative path and Git commit SHA listed in
  the Kanban initiative card, so the card serves as the single reference point
  for the active draft.

## What you must not do

- Do not leave a decision recorded in conversation but not in the draft. The
  draft is the record.
- Do not carry "open questions" into D2 without a disposition: either resolve
  them now or record them as "flagged for D3" with context.
- Do not advance to D2 on a mutable, uncommitted worktree file. The D2 dispatch
  must reference an immutable Git commit.
- Do not perform the D2 review. The D2 pass is performed by an independent
  profile (independent-reviewer). Your job in D1 is to produce a draft that is
  ready for that dispatch.

## Phase outcome and route

D1 is complete when:
- The draft reflects all decisions made during the phase
- All resolvable items are resolved in-document; items requiring ratification
  are flagged for D3 with context
- The draft is committed and pushed (if a decision changed the artifact)
- The draft is ready for D2 dispatch

Routes out of D1:
- To D2: draft is ready for iterative review
- To D1 (stay): a new decision changes the draft and the immediate-update cycle
  repeats

## Failure modes that have bitten

- **Passive D1:** The orchestrator waits for Adrian to dictate the design
  instead of probing, investigating, and shaping it. The interview dynamic is
  the core of D1; a passive orchestrator produces a thin draft that bounces
  off D2 with unexamined assumptions and missing integration analysis.
- **Decision-to-document gap:** A decision is made in conversation but the
  draft is not updated in the same step. The D2 reviewer then reviews a draft
  that doesn't reflect the agreed state. This defeats the purpose of the
  iterative loop. The immediate-update principle exists to prevent exactly this.
- **"Carried to D3" without in-document recording:** An item is marked as
  deferred to D3 but the draft says nothing about it. The D3 ratification then
  has no context for the item. Every deferred item must be visible in the draft
  with the decision context.
- **Git-publication checkpoint skipped:** The draft is revised but not committed
  before D2 dispatch. The reviewer works from a stale baseline. Always commit +
  push after a decision changes the artifact, before dispatching D2.

## Authority

- `Canon/design-lifecycle.md` §D1 — the lifecycle rule: human-led concept
  formation, immediate-update principle, Git-publication checkpoint.
- `2-design/` — the draft design document(s) for this initiative.
