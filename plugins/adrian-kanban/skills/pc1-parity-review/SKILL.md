---
name: pc1-parity-review
description: Operational guidance for PC1 milestone parity review.
version: 0.2.0
author: Adrian Lin, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    kanban_phase: PC1
    kanban_contract_id: adrian-kanban.lifecycle.pc1
    kanban_contract_version: "1"
    tags: [lifecycle, pc1, parity, milestone, review]
---

# PC1 — Milestone Design-to-Implementation Parity Review

PC1 runs after all segments in a milestone have completed DEV4 and been
merged into `origin/main`. It is an omission-detection control: it looks
across the milestone for cumulative, cross-segment, or later-visible
design consequences that no single segment's DEV4 had sufficient context
to surface. It is not a second DEV3 code or test pass and must not
reopen a DEV4.2 outcome already ratified through D1-D4 unless it
identifies a genuinely new, materially different cross-segment
consequence.

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

The orchestrator coordinates PC1. The independent-reviewer assembles
the milestone evidence and authors the parity matrix. The
test-authority-reviewer verifies the matrix against the verified design
baseline. The orchestrator synthesizes both outputs, prepares the
new-signal packages, and presents the milestone parity-review record
for human decision. Human ratification of any signal disposition is
non-delegable.

Role assignments:
- PC1.1a: independent-reviewer — evidence assembly and parity
  matrix authoring
- PC1.1b: test-authority-reviewer — matrix verification against
  design baseline
- PC1.1c: orchestrator — synthesis of matrix and verification
- PC1.2: orchestrator — new-signal package preparation and
  disposition routing
- PC1.3: human — milestone closure decision

## Milestone anchor

Before performing PC1 work, resolve the milestone scope and
evidence:

1. Read the initiative's transition chain. Confirm the initiative
   is in a post-DEV4 state: the highest-ID accepted transition for
   every included segment must be `to_phase = "PC1"` or the
   initiative must have a recorded milestone-closure trigger.
   If any included segment has not completed DEV4, stop — PC1
   is not eligible.
2. Identify the milestone scope. The milestone is the set of
   segments declared in the milestone definition (a Kanban
   initiative or a named milestone record under
   `2-design/`). Read the segment IDs, their product/module
   assignment, and their completion state from the segment
   register at `2-design/segment-register.md`.
3. Confirm the verified design baseline. The baseline is the
   canonical design at `Canon/design-lifecycle.md` plus any
   milestone-ratified design changes integrated through D4
   during the milestone. Record the baseline commit SHA or
   revision reference.
4. Confirm that `origin/main` contains every included segment's
   accepted merge. For each segment, verify:
   `git merge-base --is-ancestor <segment-merge-sha> origin/main`
   returns true. If any segment is not contained in
   `origin/main`, stop — the milestone evidence is incomplete.
5. Confirm the workspace state. PC1 is a read-only review; no
   segment worktree is required. The trusted workspace
   (`main` branch) is the read reference. The segment
   worktrees remain available for targeted inspection of
   specific segment evidence but are not the working state.

Stop conditions (fail closed):
- Any included segment has not completed DEV4.
- Any segment's merge SHA is not contained in `origin/main`.
- The verified design baseline cannot be identified by commit
  SHA or revision reference.
- The segment register does not list all included segments.
- The milestone definition is missing or ambiguous.
- The parity evidence set cannot be assembled from the
  declared sources.

## Inputs

PC1 requires the following evidence set. Each source must be
identified by product/module and included segment:

- Verified design baseline (canonical source, version or
  revision reference)
- Design changes ratified during the milestone (D4 integration
  records)
- Closed-segment register: segment identity, product/module,
  scope boundary, completion state, closure reference
- DEV3 closure evidence for each segment: current-build
  classification, review conclusion, full-cycle correction
  record where applicable
- DEV4.2 implementation-to-design reconciliation outcomes
  and their D1-D4 integration references
- Accumulated implementation context: durable technical
  decisions, constraints, assumptions, dependency
  observations, operating notes
- Cross-segment relationship record: interfaces, shared data,
  ownership boundaries, dependency edges, sequencing
  assumptions
- Deferral and future-upgrade register
- Open exception and human-decision register

If an input source is missing or incomplete, record the gap
and proceed with the available evidence. The parity matrix
will mark affected rows as `Insufficient evidence` and
PC1.1 step 4 permits targeted inspection to resolve the
named uncertainty.

## PC1.1 — Milestone Parity Evidence Assembly and
Comparison

### PC1.1a — Evidence Assembly and Matrix Authoring

The orchestrator dispatches the evidence assembly and parity
matrix authoring to the independent-reviewer.

The independent-reviewer:
1. Assembles the required PC1 inputs into a milestone evidence
   set. Distinguishes each source by product/module and
   included segment.
2. Compares the accumulated implementation and relationship
   evidence directly with the verified design baseline.
   Records the comparison in a milestone parity matrix. Each
   row states:
   - the exact design reference;
   - the relevant product/module and segment or segments;
   - implementation, context, and relationship evidence;
   - status; and
   - required action.
3. Uses exactly these statuses:
   - **Aligned:** accumulated evidence supports the existing
     design.
   - **Already ratified:** an earlier DEV4.2 outcome has
     already reconciled the difference through D1-D4. Record
     the reference; do not reopen it merely because PC1 sees
     it.
   - **Possible quiet signal:** accumulated or cross-segment
     evidence may have a design consequence that no earlier
     segment identified.
   - **Insufficient evidence:** available records cannot
     establish parity.
4. For each `Insufficient evidence` row, conducts only the
   targeted, read-only source or artefact inspection needed to
   resolve the named parity uncertainty. Records the reason,
   exact material inspected, and conclusion. This is not
   permission to repeat broad code review or full test
   execution.
5. Delivers the completed matrix and a list of every new
   `Possible quiet signal` to the orchestrator.

The parity matrix follows the shape in the PC1 reference
(`references/pc1-parity-matrix.md` in the design-lifecycle
skill):

| Design reference | Product / module | Relevant
  segment(s) | Implementation, context, and relationship
evidence | Status | Required action |
|---|---|---|---|---|---|

Status values: `Aligned`, `Already ratified`, `Possible quiet
signal`, `Insufficient evidence`.

The targeted inspection log records:

| Matrix row / design reference | Why existing evidence
  is insufficient | Exact source or artefact inspected |
  Read-only conclusion | Resulting status |
|---|---|---|---|---|

### PC1.1b — Matrix Verification

The orchestrator dispatches the completed matrix to the
test-authority-reviewer for verification against the verified
design baseline.

The test-authority-reviewer:
1. Re-reads the verified design baseline at the recorded
   commit or revision.
2. Checks each parity matrix row against the design reference:
   - Is the design reference correctly cited?
   - Is the evidence sufficient to support the assigned status?
   - Are there rows that the independent-reviewer missed
     (design references not covered by the matrix)?
   - Are there rows where the status is wrong (e.g., a
     `Possible quiet signal` that is actually `Aligned`)?
3. Records verification findings: confirmed rows, corrected
   rows, and missing rows with the required correction.
4. Delivers the verification record to the orchestrator.

### PC1.1c — Synthesis

The orchestrator synthesizes the independent-reviewer's matrix
and the test-authority-reviewer's verification record.

The orchestrator:
1. Applies the test-authority-reviewer's corrections to the
   matrix.
2. Records any disagreement between the two reviews with a
   resolution rationale.
3. Confirms the final matrix is complete: every design
   reference in the verified baseline has a row, and every row
   has a justified status.
4. Produces the final milestone parity-review record including
   the matrix, the targeted inspection log, and the new-signal
   list.
5. Commits the record to `2-design/<milestone-id>/` or the
   milestone-declared evidence location.

## PC1.2 — New-Signal Package and Disposition Route

The orchestrator prepares a focused design-return package for
each new material signal from PC1.1.

Each package contains:
- Stable signal identifier (PC1-<milestone>-<seq>)
- Affected product/module and every contributing segment
- Exact governing design source and statement
- Observed accumulated implementation reality, supported by
  evidence
- Why DEV4 did not capture the observation: cross-segment
  consequence, later dependency discovery, cumulative effect,
  incomplete earlier visibility, or another recorded reason
- Behavioural, architectural, dependency, ownership, data,
  operational, and future-upgrade impact, as applicable
- Classification: design gap, design ambiguity, design-
  relevant decision, or non-design implementation detail
- Recommended route
- Recommended Reflect, Drop, Defer, or Reject disposition
  where the signal calls for implementation-to-design
  reconciliation; this remains a proposal, not PC1 authority
- Required dependencies, affected design sources, and any
  named future upgrade/stage
- Evidence references to the parity matrix, segment records,
  implementation context, and any targeted inspection

Route each package as follows:

| Signal classification | Required route |
|---|---|
| Design gap or design-relevant decision | D1-D4 focused
  design revision and ratification path |
| Design ambiguity | Stop for explicit human decision through
  D3; do not guess |
| Existing ratified design violated | Return affected
  implementation to DEV3 for correction; do not normalize the
  divergence |
| Non-design implementation detail | Record cited rationale
  and retain only in durable implementation context |

PC1 only recommends a disposition. D1-D4 and human ratification
decide it. The orchestrator does not apply any design change,
create any correction card, or route any implementation to
DEV3 during PC1. It prepares the packages and presents them.

The orchestrator presents each signal package to Adrian with:
- the signal identifier and classification
- the governing design citation
- the observed reality and why DEV4 missed it
- the recommended route and disposition
- the impact summary
- the evidence references

Adrian selects the outcome for each signal. The orchestrator
records the decision and the route.

## PC1.3 — Milestone Closure Decision

The orchestrator presents the completed PC1 record for
milestone closure decision.

The milestone may be marked:

- **Clear:** every new material signal has entered D1-D4 with
  a complete design-return package, been stopped for explicit
  human decision, returned affected work to DEV3 for
  correction, or been evidenced and recorded as a non-design
  implementation detail. Previously ratified DEV4.2 outcomes
  remain settled.
- **Conditionally open:** one or more signals are retained
  open against a recorded D1-D4 or future-upgrade dependency.
  The milestone remains open against those dependencies.
- **Blocked:** the required human decision or DEV3 correction
  outcome is pending. The milestone is blocked until the
  dependency is resolved.

The closure statement is recorded in the milestone parity-
review record and committed to the milestone evidence location.

## Durable records

PC1 produces these records:

- Milestone evidence set (assembled inputs with source
  identification)
- Milestone parity matrix (final, verified)
- Targeted inspection log
- New-signal packages (one per material signal)
- Milestone parity-review record (complete PC1 output)
- Milestone closure statement

All records are committed to the milestone-declared evidence
location under `2-design/` or the segment register's declared
path. The records are read-only references for subsequent
milestones and PC1 re-runs.

## Kanban integration

- Kanban: PC1.1a and PC1.1b create child cards linked to the
  initiative. PC1.1c, PC1.2, and PC1.3 are orchestrator and
  human actions without child cards.
- Transition records: the PC1 entry transition
  (`DEV4 -> PC1`) is recorded when the last segment in the
  milestone completes DEV4. The PC1 completion transition
  (`PC1 -> milestone-closed` or `PC1 -> next-milestone`) is
  recorded after the closure statement is committed.
- Task cards: PC1 cards carry `segment_id` for every included
  segment in the milestone, `lifecycle_phase = "PC1"`, and
  the milestone definition reference.
- Evidence association: the parity matrix, inspection log,
  signal packages, and closure statement are committed to the
  milestone evidence location and referenced in the
  transition record.

## Authority

- `Canon/design-lifecycle.md` Part III (PC1) is the
  authoritative procedure. This skill operationalizes it;
it does not add, remove, or reinterpret lifecycle rules.
- Canon Core Rule 1: the verified design baseline is the
  authority for the parity comparison. Agent agreement, task
  briefs, test output, or implementation convenience do not
  supersede it.
- Canon Core Rule 4: a required independent review must be
  independently formed; it is not satisfied by a self-review
  or a restatement of another review. PC1.1a and PC1.1b are
  independently formed reviews.
- Canon Core Rule 5: a design gap, design ambiguity, or
  design-relevant decision must be routed to the design
  lifecycle. It must not be silently settled in
  implementation.
- Canon Core Rule 7: a stage may advance only after its
  stated exit gate is satisfied.
- PC1 must not repeat DEV3 test execution or broad code
  review.
- PC1 must not reopen an implementation-to-design outcome
  already ratified through DEV4.2 and D1-D4, unless it
  identifies a genuinely new, materially different
  cross-segment consequence.
- PC1 must not treat accumulated implementation as automatic
  design authority.
- Targeted inspection is read-only, evidence-limited, and
  permitted only to resolve a named insufficient-evidence
  row.
- PC1 must not classify a material signal as an
  implementation detail without cited rationale.
- PC1 may recommend a Reflect, Drop, Defer, or Reject
  outcome; only the focused D1-D4 path and human ratification
  may decide it.
