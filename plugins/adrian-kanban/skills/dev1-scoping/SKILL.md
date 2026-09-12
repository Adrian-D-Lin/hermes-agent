---
name: dev1-scoping
description: Operational guidance for DEV1 scope definition.
version: 0.2.0
author: Adrian Lin, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    kanban_phase: DEV1
    kanban_contract_id: adrian-kanban.lifecycle.dev1
    kanban_contract_version: "1"
    tags: [lifecycle, dev1, scope-definition, reconnaissance, segmentation]
    display_priority: 100
---

# DEV1 - Scope Definition

DEV1 runs after D4 integration. It transforms a verified design baseline
into a ratified, implementation-ready scope package: multi-angle
reconnaissance, iterative consolidation, independent scope check,
segmentation, independent segment check, and per-segment
dispatch-readiness confirmation.

## Trusted initiative anchor

Before phase work, use the server-issued Session Startup anchor for a top-level
`default` session, or the trusted dispatch binding for a worker. Verify the
selected project, `initiative_id`, current phase, coordination workspace ID,
member repositories, exact derived roots, and binding version against the
Initiative Tracker. Do not infer them from the session CWD, chat text, display
title, or filesystem discovery. The controller creates and repairs coordination
worktrees; work only in the roots in the binding. Concurrent bindings are
permitted, so refresh repository state before edits and recheck the complete
diff and HEAD before committing.

## Your role

The orchestrator coordinates all of DEV1. Specialist profiles perform the
investigative and independent-check steps; the orchestrator performs the
merge, iterate-to-dry, and dispatch-readiness steps.

Role assignments:
- DEV1.1 (a-e): independent-reviewer - five sequential individual dispatches
- DEV1.2: orchestrator - mechanical merge of all angle findings
- DEV1.3: orchestrator - iterate-to-dry loop with IR follow-up dispatches
- DEV1.4: test-authority-reviewer - Design-to-Scope check
- DEV1.5: independent-reviewer - segmentation
- DEV1.6: test-authority-reviewer - Scope-to-Segments check
- DEV1.7: orchestrator - per-segment dispatch-readiness confirmation

## DEV1.1 - Multi-angle Reconnaissance Sweep

The orchestrator dispatches all five reconnaissance angles to the
independent-reviewer profile. Each angle is a separate Kanban card,
dispatched one at a time in sequence. Do not combine multiple angles into
a single card or dispatch them in parallel. Each angle must complete and
its output must be captured before the next angle is dispatched.

The five angles in order:
1. Design-source angle - inspect the verified design baseline and
   directly governing sources.
2. Existing-code angle - inspect current implementation, conflicts,
   reuse opportunities, and content fit.
3. Prior-context angle - inspect relevant earlier build summaries,
   trace records, and implementation history.
4. Dependency and relationship angle - inspect cross-module
   dependencies, coupling, and relationship edges.
5. Execution and dispatch feasibility pre-flight - identify provisional
   dispatch-shape constraints, required inputs, source-path uncertainty,
   task-boundary risks, artefact-lifecycle constraints, and
   prerequisites. This angle asks: what could prevent this work from
   being carried out as currently understood? It is evidence for the
   scope investigation, not a final pass/fail check.

Each card output: independent angle findings with identified
uncertainties, assumptions, follow-up probes, and cited evidence.

Dispatch protocol (per angle):
- Create one Kanban card per angle, associated with the parent
  initiative.
- The card names the profile (independent-reviewer) and the exact
  angle to investigate.
- The card identifies the exact immutable design baseline (path +
  commit SHA recorded at D4) and the current branch/commit of the
  working repository (the baseline commit and whether the worktree
  is clean or has uncommitted changes). The baseline pointer is
  whatever D4 produced: a single file path + SHA, a set of file
  paths + SHAs, or a tree state reference. The card must name the
  specific pointer form so the worker knows exactly what to
  verify.
- The card states what the angle must produce: findings with
  citations, uncertainties, assumptions, and follow-up probes.
- The card must NOT reference findings from any other angle. Each
  angle works from the shared baseline independently.
- Wait for the accepted completion or recorded orchestration checkpoint
  named by the lifecycle contract before dispatching the next angle.
  `blocked`, `archived`, worker termination, or a terminal-looking card
  alone does not release the sequence. An explicit, separately approved
  human gate override may release it while preserving the unmet criterion;
  never represent that override as an accepted reviewer verdict.
- Capture the output (card ID, findings, evidence) for DEV1.2 merge.

## DEV1.2 - Reconnaissance Merge

The orchestrator mechanically merges all five DEV1.1 angle findings
into one cumulative reconnaissance record. Do not discard materially
distinct findings, uncertainties, assumptions, or follow-up probes. The
merge is mechanical: combine without editorial judgment. Each finding
retains its source angle label and card ID.

Output: Cumulative reconnaissance record with all five angle findings
preserved.

## DEV1.3 - Iterate-to-Dry

The orchestrator inspects the cumulative reconnaissance record for
open probes, evidence gaps, unresolved dependency questions,
design-silence signals, and cross-angle implications.

For each open item:
- Route the follow-up through the angle whose lens raised it. Do not
  collapse distinct concerns merely because they request the same
  source.
- Create a targeted follow-up card for the independent-reviewer,
  scoped to the specific open item, associated with the parent
  initiative.
- The follow-up card references the original angle finding and the
  specific question to resolve.
- Wait for completion, capture the output, and merge it into the
  cumulative reconnaissance record.

Repeat until a complete novelty check produces no new material scope
investigation.

Novelty check protocol:
- A complete novelty check re-sweeps all five angles' accumulated
  findings against the current cumulative record. Each angle is
  re-examined for new design-silence signals, cross-angle
  implications, or unexplored probes that the prior pass did not
  cover.
- A finding is material if it changes the proposed scope, identifies
  an unaddressed design gap or silence, or surfaces a cross-angle
  contradiction not yet resolved. Trivial restatements, duplicate
  findings already in the record, and observations that do not alter
  the scope boundary are not material.
- The novelty check is performed by the orchestrator. It is not
  delegated to a reviewer. The orchestrator's determination that no
  new material finding emerged is the dry declaration.
- Record the pass counter: which pass number produced the dry
  declaration. The dry declaration includes the pass number, the
  date, and a statement that a complete five-angle novelty check
  found no new material finding.

The dry declaration is the orchestrator determination that the novelty
check found nothing new. It is not a reviewer verdict.

Output: Dry cumulative reconnaissance record and dry declaration.

## DEV1.4 - Design to Scope Check

The orchestrator dispatches the dry cumulative reconnaissance record
to the test-authority-reviewer profile for an independent check
against the verified design baseline.

TAR confirms that the proposed scope is grounded in the verified design
baseline and identifies any design divergence, design silence, or
unsupported scope claim.

Output: Ratified reconnaissance and scope record, or findings
requiring return to investigation or the design lifecycle. When the
check passes, the orchestrator assembles the ratified scope record from
the dry cumulative reconnaissance record and the TAR confirmation,
freezing it with a path and commit SHA or Kanban card reference. This
frozen record is the input to DEV1.5 and DEV1.6.

Dispatch protocol:
- One Kanban card to test-authority-reviewer, associated with the
  parent initiative.
- The card identifies the exact immutable design baseline (path +
  commit SHA recorded at D4) and the dry cumulative reconnaissance
  record.
- The card states what TAR must produce: a Design-to-Scope check
  result, or findings requiring return to investigation or the design
  lifecycle.
- The baseline path must be reachable in the card's worktree or
  attached as a content-addressed bundle with a stated SHA the worker
  must verify before starting.

Route: If TAR finds a design gap, ambiguity, or design-relevant
decision, the affected scope path stops and returns to D1-D4. DEV1 must
not invent design authority to close the issue.

TAR finding disposition: When TAR returns findings, the orchestrator
classifies each finding against the design baseline. Design gaps,
design ambiguity, or design-relevant decisions return to D1-D4. Scope
grounding issues that can be resolved within the reconnaissance record
are resolved in DEV1.2/DEV1.3 and the check is re-dispatched. The
orchestrator does not adopt TAR's classification; it classifies
independently against the Canon finding routes.

## DEV1.5 - Segmentation

The orchestrator dispatches the ratified scope record to the
independent-reviewer profile for segmentation.

IR divides the work into either one atomic segment or a
dependency-aware sequence of independently actionable segments. IR
sequences the work to enable progressive verification where possible.

Dispatch protocol:
- One Kanban card to independent-reviewer, associated with the parent
  initiative.
- The card identifies the exact ratified scope record (path + commit
  SHA or card reference).
- The card states what IR must produce: the proposed segment list,
  boundaries, dependencies, ordering rationale, and
  progressive-verification opportunities.

## DEV1.6 - Scope to Segments Check

The orchestrator dispatches the proposed segment list to the
test-authority-reviewer profile for an independent check against the
ratified scope record.

TAR tests for missed coupling, false separation, unnecessary coupling,
unmet prerequisites, and sequencing that prevents the intended
progressive verification.

Dispatch protocol:
- One Kanban card to test-authority-reviewer, associated with the
  parent initiative.
- The card identifies the exact ratified scope record and the
  proposed segment list.
- The card states what TAR must produce: a ratification of the
  segment list or required segmentation revisions.

## DEV1.7 - Per-segment Dispatch-Readiness Confirmation

Immediately before a ratified segment proceeds to DEV2, the
orchestrator confirms that its exact work package is actionable:

- The segment boundary and dependencies are ratified and satisfied or
  explicitly gated.
- Required design and scope sources are identified and available.
- The handoff package is complete.
- No open uncertainty is disguised as an implementation instruction.
- No known execution or dispatch-blocking condition remains.

This is a final readiness confirmation against the actual segment being
briefed. It is distinct from DEV1.1's provisional feasibility
pre-flight.

Output: Dispatch-ready segment package, or a returned issue with the
stage that must resolve it.

## Finding routes

- Evidence or repository uncertainty: return to targeted
  investigation within DEV1.3.
- Scope defect: return to the relevant DEV1.1-DEV1.3 investigation,
  then repeat DEV1.4. Route to the step that owns the defect's origin:
  DEV1.1 if the defect is a missed fact the angles should have
  surfaced (evidence gap, uncited assumption, unexplored design
  silence); DEV1.2 if the defect is a merge error (finding dropped
  or mislabeled during the mechanical merge); DEV1.3 if the defect
  is an unresolved open probe that the iterate-to-dry loop failed
  to close.
- Segmentation defect: revise DEV1.5, then repeat DEV1.6.
- Design gap, design ambiguity, or design-relevant decision: stop the
  affected scope path and return to D1-D4. DEV1 must not invent design
  authority to close the issue.
- Dispatch-readiness failure: return the issue to the earliest stage
  that can resolve it; do not proceed to DEV2 on a known unready
  package.

## Records

- Cumulative reconnaissance record: maintained in the orchestrator's
  session context; the dry declaration is committed to main as a
  durable DEV1 artefact under `2-design/`.
- Ratified reconnaissance and scope record: the dry cumulative record
  plus the TAR confirmation, frozen with a path and commit SHA.
  Committed to main before DEV1.5 dispatch.
- Segment list and per-segment readiness confirmations: committed to
  main as part of the DEV1 closure evidence.
- Kanban: the DEV1 phase is tracked on the parent initiative card.
  Each dispatch (DEV1.1a-e, DEV1.4, DEV1.5, DEV1.6) creates a child
  card linked to the initiative. DEV1.7 is the orchestrator's final
  confirmation, recorded on the initiative card.

## Phase outcome and route

DEV1 is complete when:
- All five DEV1.1 angles have been dispatched individually and
  completed.
- The reconnaissance record has been merged (DEV1.2) and iterated
  to dry (DEV1.3).
- The Design-to-Scope check (DEV1.4) has been completed by TAR.
- The scope record is ratified and the segment list is proposed
  (DEV1.5).
- The Scope-to-Segments check (DEV1.6) has been completed by TAR.
- Each segment entering DEV2 has passed its own
  dispatch-readiness confirmation (DEV1.7).
- No unresolved design gap, design ambiguity, design-relevant
  decision, or known dispatch-blocking condition remains embedded in
  any segment package.
- The Kanban initiative card is updated with the DEV1 closing
  result.

Each dispatch-ready segment proceeds to DEV2 when its declared
dependencies permit.

## Failure modes

- Combined angle dispatch: The orchestrator creates one card covering
  two or more DEV1.1 angles, or dispatches multiple angle cards in
  parallel. Each angle is a separate card, dispatched sequentially,
  with the prior angle output captured before the next is created.
- Parallel angle fan-out: The orchestrator dispatches all five
  angle cards at once to save time. The sequence requirement is
  intentional: each angle's findings may inform follow-up probes in
  DEV1.3, and the cumulative record is built incrementally.
- Silent drop of a recon finding: A materially distinct finding from
  one angle is omitted from the DEV1.2 merge or the DEV1.3
  cumulative record. The merge must preserve all findings; the item
  count in the dry record must account for all five angles' outputs.
- Provisional feasibility treated as final readiness: DEV1.1e's
  provisional dispatch-feasibility finding is treated as a DEV1.7
  dispatch-readiness confirmation. DEV1.7 is a separate, final check
  against the actual segment package.
- Task convenience driving scope boundaries: The orchestrator merges,
  splits, or reorders segments because a single card is easier to
  dispatch, or because a particular split is easier to verify. Scope
  boundaries must derive from the reconnaissance record and the
  ratified scope, not from dispatch convenience.
- Segmentation from unratified scope: DEV1.5 segments are proposed
  from a scope record that has not passed the DEV1.4 check. The
  scope record must be ratified before segmentation.

## Authority

- Canon/design-lifecycle.md section DEV1 - Scope Definition
  procedure, finding routes, outputs, boundaries, exit gate, and
  route.
- Canon/design-lifecycle.md Core Rule 4 - independent review
  requirement (applies to DEV1.1 angle dispatches, DEV1.3
  follow-ups, DEV1.4, DEV1.5, and DEV1.6 dispatches).
- Canon/design-lifecycle.md Core Rule 5 - design gap routing
  (applies to DEV1.4 and DEV1.6 finding routes).
- Canon/design-lifecycle.md Core Rule 7 - lifecycle advancement
  discipline (applies to the DEV1.7 exit gate and the DEV2
  handoff).
- The verified design baseline (path + commit SHA recorded at D4).
- The Kanban initiative card - the single reference point for the
  initiative's lifecycle state and DEV1 closing result.
