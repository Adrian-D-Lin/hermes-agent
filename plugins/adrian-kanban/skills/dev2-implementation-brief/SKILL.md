---
name: dev2-implementation-brief
description: Operational guidance for DEV2 implementation brief.
version: 0.2.0
author: Adrian Lin, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    kanban_phase: DEV2
    kanban_contract_id: adrian-kanban.lifecycle.dev2
    kanban_contract_version: "1"
    tags: [lifecycle, dev2, implementation-brief, design-to-brief-check, brief-handoff]
---

# DEV2 - Implementation Brief

DEV2 runs after DEV1. It translates one dispatch-ready segment into a
concrete, cited implementation brief without creating design authority:
brief drafting, independent design-to-brief check, and brief
convergence with handoff to DEV3.

## Your role

The orchestrator coordinates DEV2. The independent-reviewer drafts the
brief, the test-authority-reviewer performs the independent check, and
the orchestrator resolves defects and issues the final brief.

Role assignments:
- DEV2.1: independent-reviewer - brief drafting
- DEV2.2: test-authority-reviewer - Design to Brief check
- DEV2.3: orchestrator - brief convergence and handoff

## Segment anchor

Sequence release requires the accepted completion or recorded orchestration
checkpoint named by the lifecycle contract. `blocked`, `archived`, worker
termination, or a terminal-looking status alone does not release the next
step. Only the separate, exactly approved human gate-override route may
release an unmet criterion, preserving it without fabricating a reviewer verdict.

Before performing phase work, resolve the active segment and workspace:

1. Read the initiative's transition chain. The active phase is the
   `to_phase` of the highest-ID accepted transition. The active segment
   is the `to_segment_id` of the highest-ID accepted transition (NULL if
   the initiative is pre-segmentation).
2. Confirm the active phase matches this skill. If it does not, stop —
   you are in the wrong phase.
3. Resolve the active segment against the segment register at
   `2-design/segment-register.md` (Git, read-only reference). Read the
   segment's identity, boundary, dependencies, dispatch package
   reference, isolation mechanism, and current state.
4. Query task cards for this `initiative_id` + `segment_id` + this
   lifecycle phase. Apply assignment, execution-status, and dependency
   rules to determine the eligible task.
5. If exactly one task is eligible for this profile, anchor to it. If
   multiple tasks are eligible, present them for selection. If none is
   eligible, report the missing or blocked work.
6. The plugin—not the caller—derives the physical segment root from the
   active `segment_workspace_id` in the task's immutable
   `lifecycle_contract_v1` and the trusted repository registry. The task
   card projection exposes `lifecycle_phase`, `segment_id`, and
   `segment_workspace_id`; an absolute path is not accepted from the task
   caller. On dispatch, the controller checks the worktree/repository
   identity, the expected branch, and required base ancestry. Multiple
   sessions may share the same logical workspace; every session must refresh
   the repository state before editing and recheck the complete diff and HEAD
   before committing. Legitimate commits may advance HEAD after
   materialization.
7. Read the task's `task_input_manifest_v1`, which exactly covers the
   declared reference paths (`baseline_refs`, `governing_source_refs`,
   `prior_record_refs`, and, after the first sequence item,
   `predecessor_ref`). Each entry is pinned by `sha256` and either a Git
   `source_locator` or a stored snapshot attachment; `context_ref` gives
   the reading instruction for that entry.
8. Retain the tuple `initiative_id + segment_id + phase + task_id +
   worktree_path + binding` as the session anchor. Revalidate it before
   governed mutations. If the initiative has advanced (a newer
   transition exists), stop and re-anchor rather than continuing against
   stale state.

**Stop conditions.** If any of the following is true, stop and report the
specific gap. Do not guess, do not pick a segment, do not create a
worktree:

- The transition chain has no DEV2-DEV4 transition (initiative is
  pre-segmentation).
- The active segment is not in the segment register.
- The task card's `segment_id` does not match the active segment from the
  chain.
- The task card's `segment_workspace_id` is missing from the
  `lifecycle_contract_v1`, or the system-derived segment root cannot be
  resolved through the trusted repository registry.
- The worktree does not exist, is on the wrong branch, fails required
  base ancestry, or has dirty/divergent state that cannot be attributed and
  safely reconciled.
- The task's `task_input_manifest_v1` does not exactly cover the
  declared reference paths, or an entry's `sha256` / `source_locator`
  pin does not verify.
- The segment's dependencies are not all closed.

### Phase-specific continuation (DEV2)

- Work only from the active segment's dispatch-ready package (the Git
  path and SHA in the segment register's `dispatch_package_ref`).
- Produce the implementation brief for this segment only. The brief must
  state the segment identity, declared boundary, and all requirements
  from the dispatch package.
- Commit the brief and check record to `2-design/<segment-id>/` (Git).
- The segment workspace is writable for the brief's required
  verification (e.g., reading live code). No implementation code is
  written in DEV2.
- Do not work on any other segment. Do not create work for S2 while S1
  is in DEV2.

## DEV2.1 - Brief Drafting

The orchestrator dispatches the dispatch-ready segment package to the
independent-reviewer profile for brief drafting.

The independent-reviewer translates the ratified segment into a
candidate implementation brief. The brief must state:
- segment identity and declared boundary;
- objective;
- direct design citations for every material requirement;
- required implementation outcomes;
- acceptance criteria;
- required files or affected surfaces, where supported by evidence;
- constraints and dependencies;
- non-goals;
- forbidden actions; and
- required verification.

Any unresolved item must be explicitly routed. It must not be buried
as an implementation instruction.

Dispatch protocol:
- One Kanban card to independent-reviewer, associated with the parent
  initiative.
- The card identifies the exact dispatch-ready segment package (path
  + commit SHA from DEV1.7) and the verified design baseline (path +
  commit SHA recorded at D4). Its `task_input_manifest_v1` pins those
  declared reference paths by `sha256` and Git `source_locator`, and its
  `context_ref` gives the reading instruction for each entry.
- The card states what the independent-reviewer must produce: a
candidate implementation brief with the nine required sections. On
completion the candidate submission is recorded as an immutable
`task_candidate_handoffs` record.
- The card must NOT reference findings from other segments or
  initiatives. Each brief is scoped to its own segment.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV2.2; terminal status alone is insufficient.
- Capture the candidate brief for DEV2.2 dispatch.

## DEV2.2 - Design to Brief Check

The orchestrator dispatches the candidate brief to the
test-authority-reviewer profile for an independent check against the
verified design baseline.

The test-authority-reviewer independently compares the candidate
brief directly with the verified design baseline. The dispatch-ready
scope package is required context, but the design baseline remains the
comparison oracle.

The test-authority-reviewer checks:
- citation fidelity: every material requirement accurately follows from
  its cited source;
- scope fidelity: the brief remains inside the ratified segment
  boundary;
- completeness: required behaviours, constraints, acceptance
  criteria, dependencies, and verification are not omitted;
- authority boundary: the brief has not introduced an unauthorized
  design decision, preference, or assumption;
- implementation feasibility: the brief does not contradict the
  DEV1.7 dispatch-ready package;
- verification adequacy: the brief requires appropriate verification
  without deriving expected behaviour from the implementation.

Dispatch protocol:
- One Kanban card to test-authority-reviewer, associated with the
  parent initiative.
- The card identifies the exact candidate brief (path + commit SHA or
  card reference) and the verified design baseline (path + commit SHA
  recorded at D4). Its `task_input_manifest_v1` pins those declared
  reference paths by `sha256` and Git `source_locator`, and its
  `context_ref` gives the reading instruction for each entry.
- The card states what the test-authority-reviewer must produce: a
Design to Brief Check record with accepted conclusions or required
findings. Reviewer acceptance is recorded as an immutable
`task_reviewer_verdicts` record.
- The baseline path must be reachable in the card's worktree or
  attached as a content-addressed bundle with a stated SHA the worker
  must verify before starting.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV2.3; terminal status alone is insufficient.
- Capture the check record for DEV2.3 resolution.

The test-authority-reviewer finding disposition: When the
test-authority-reviewer returns findings, the orchestrator classifies
each finding against the design baseline. Design gaps, design
ambiguity, or design-relevant decisions return to D1-D4.
Implementation-level brief defects are resolved in DEV2.3 and the
check is re-dispatched. The orchestrator does not adopt the
test-authority-reviewer's classification; it classifies independently
against the Canon finding routes.

## DEV2.3 - Brief Convergence and Handoff

The orchestrator performs the convergence loop and handoff:

1. For an implementation-level brief defect, revise the candidate
   brief and repeat DEV2.2.
2. Dispose of every Design to Brief Check finding.
3. Confirm that the final brief is the version that passed the check.
4. Confirm that no design issue remains concealed as an
   implementation instruction.
5. Record the accepted orchestration checkpoint for this segment. The
   DEV3 first task carries that resulting accepted record through its
   exact `predecessor_ref`, and its `task_input_manifest_v1` pins the
   accepted brief file through its Git `source_locator` and `sha256`.

Output: Ratified implementation brief and brief-ratification record.

## Finding routes

- Implementation-level brief defect: return to DEV2.1, then repeat
  DEV2.2. The orchestrator revises the candidate brief in place and
  re-dispatches the Design to Brief Check.
- Design gap or design-relevant decision: return to D1-D4 before the
  affected segment enters DEV3. DEV2 must not invent design authority
  to close the issue.
- Design ambiguity: stop for an explicit human decision through D3;
  do not guess.

## Records

- Candidate implementation brief: maintained in the orchestrator's
  session context during the convergence loop; the final ratified
  brief is committed to main as a durable DEV2 artefact under
  `2-design/<segment-id>/`.
- Design to Brief Check record: committed to main under
  `2-design/<segment-id>/` as part of the DEV2 closure evidence.
- Brief-ratification record: committed to main under
  `2-design/<segment-id>/` as part of the DEV2 closure evidence.
- Kanban: the DEV2 phase is tracked on the parent initiative card.
  Each dispatch (DEV2.1, DEV2.2) creates a child card linked to the
  initiative. DEV2.3 is the orchestrator's final convergence and
  handoff, recorded on the initiative card as the accepted orchestration
  checkpoint. Candidate submissions are immutable `task_candidate_handoffs`;
  reviewer acceptance is immutable `task_reviewer_verdicts`. Sequence
  release is through the exact immutable `predecessor_ref`: either an
  accepted candidate handoff/reviewer verdict or an accepted
  orchestration checkpoint, as declared by the lifecycle contract.

## Phase outcome and route

DEV2 is complete when:
- The candidate brief has been drafted by the independent-reviewer
  (DEV2.1).
- The Design to Brief Check has been completed by the
test-authority-reviewer (DEV2.2).
- All check findings have been disposed of (DEV2.3).
- The final brief is the version that passed the check.
- No design gap, ambiguity, or design-relevant decision remains
  embedded in the brief.
- The brief-ratification record is committed to main.
- The Kanban initiative card is updated with the DEV2 closing
  result.

The ratified brief proceeds to DEV3 when the segment's declared
dependencies permit.

## Failure modes

- Design authority in the brief: The independent-reviewer-drafted
brief introduces an unauthorized design decision, preference, or
assumption not grounded in the verified design baseline. The brief is
an implementation translation, not a design oracle. DEV2.2 must catch
this; DEV2.3 must not silently resolve it.
- Buried unresolved item: An unresolved item is stated as an
  implementation instruction rather than being explicitly routed. The
  brief must say where the item goes, not hide it in the build
  package.
- Brief revision without re-check: The orchestrator revises the
  candidate brief after DEV2.2 findings and issues it to DEV3 without
  repeating the Design to Brief Check. Every revised brief must pass
  DEV2.2 again.
- Segment boundary drift: The brief exceeds the ratified segment
  boundary from DEV1. The brief must stay inside the declared
  boundary; if the work is larger, that is a scope defect routed to
  DEV1, not a brief-level fix.
- Verification derived from implementation: The brief's verification
  section derives expected behaviour from the implementation rather
  than from the design. Verification must check that the
  implementation matches the design, not that the implementation
  matches itself.

## Authority

- Canon/design-lifecycle.md section DEV2 - Implementation Brief
  procedure, finding routes, outputs, boundaries, exit gate, and
  route.
- Canon/design-lifecycle.md Core Rule 4 - independent review
  requirement (applies to DEV2.1 brief drafting and DEV2.2 Design to
  Brief Check dispatches).
- Canon/design-lifecycle.md Core Rule 5 - design gap routing
  (applies to DEV2.2 finding routes).
- Canon/design-lifecycle.md Core Rule 7 - lifecycle advancement
  discipline (applies to the DEV2.3 exit gate and the DEV3
  handoff).
- The verified design baseline (path + commit SHA recorded at D4).
- The dispatch-ready segment package (path + commit SHA from
  DEV1.7).
- The segment register at `2-design/segment-register.md` - the
  read-only reference for segment identity, boundary, dependencies,
  dispatch package reference, and current state.
- The task's immutable `lifecycle_contract_v1` (`segment_id`,
  `segment_workspace_id`, `baseline_refs`, `governing_source_refs`,
  `prior_record_refs`, and, after the first sequence item,
  `predecessor_ref`) and its `task_input_manifest_v1` (each entry pinned
  by `sha256` and a Git `source_locator` or stored snapshot attachment;
  `context_ref` gives the reading instruction). The plugin—not the
  caller—derives the physical segment root from the active
  `segment_workspace_id` and the trusted repository registry; on
  dispatch the controller checks worktree/repository identity, expected
  branch, and required base ancestry. Concurrent bindings are visible and
  permitted; stale diffs, Git conflicts, and unexplained advancement fail
  closed rather than being overwritten.
- The Kanban initiative card - the single reference point for the
  initiative's lifecycle state and DEV2 closing result.
