---
name: dev4-closure
description: Operational guidance for DEV4 closure and integration.
version: 0.2.0
author: Adrian Lin, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    kanban_phase: DEV4
    kanban_contract_id: adrian-kanban.lifecycle.dev4
    kanban_contract_version: "1"
    tags: [lifecycle, dev4, closure, archival, integration]
---

# DEV4 — Closure and Capture

DEV4 runs after DEV3. It closes the segment by disposing of eligible
development/testing byproducts, reconciling implementation-to-design
observations through explicit human ratification, merging the
segment into `origin/main`, retiring the segment workspace, and
admitting the next segment or closing the initiative.

## Your role

The orchestrator coordinates DEV4. The independent-reviewer performs
archival candidate scanning and authors the ratification package.
The test-authority-reviewer verifies the
archival disposition. The builder-tester executes relocation. The
orchestrator drives the focused D1-D4 ratification path and presents
ratification for human decision. Human ratification (DEV4.2c) is
non-delegable.

Role assignments:
- DEV4.1a: independent-reviewer — archival-candidate scanning
- DEV4.1b: test-authority-reviewer — archival verification
- DEV4.1c: builder-tester — archival disposition execution
- DEV4.2a: independent-reviewer — ratification package authoring
- DEV4.2b: orchestrator — focused design-integration review and
  presentation
- DEV4.2c: human — design ratification
- DEV4.2d: orchestrator — Write-Gated integration or DEV3 return
- DEV4.2e: orchestrator — implementation-context capture
- DEV4.3: orchestrator — segment merge and integration
- DEV4.4: orchestrator — workspace retirement
- DEV4.5: orchestrator — next-segment admission

## Segment anchor

Sequence release requires the accepted completion or recorded orchestration
checkpoint named by the lifecycle contract. `blocked`, `archived`, worker
termination, or a terminal-looking status alone does not release the next
step. Only the separate, exactly approved human gate-override route may
release an unmet criterion, preserving it without fabricating a reviewer verdict.

Before performing phase work, resolve the active segment and workspace:

1. Read the initiative's transition chain. The active phase is the
   `to_phase` of the highest-ID accepted transition. The active segment
   is the `to_segment_id` of the highest-ID accepted transition (NULL
   if the initiative is pre-segmentation).
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

**Stop conditions.** If any of the following is true, stop and report
the specific gap. Do not guess, do not pick a segment, do not create a
worktree:

- The transition chain has no DEV2-DEV4 transition (initiative is
  pre-segmentation).
- The active segment is not in the segment register.
- The task card's `segment_id` does not match the active segment from
  the chain.
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
- The DEV3 exit gate has not been satisfied for this segment.

### Phase-specific continuation (DEV4)

- The DEV3 exit gate must be satisfied before DEV4 work begins.
  No unresolved remediable implementation finding may remain.
- DEV4 performs segment-local closure: archival, design
  reconciliation, merge, and workspace retirement for this segment
  only. Cross-segment or cross-initiative cleanup belongs to PC1.
- The segment workspace is the working directory for DEV4 archival
  and merge work. Do not create a separate worktree for DEV4.
- DEV4 evidence (archival records, ratification packages, merge
  evidence) is committed under `2-design/<segment-id>/`.

## DEV4.1a — Archival-Candidate Scanning

The orchestrator dispatches the touched-scope file list to the
independent-reviewer for archival-candidate scanning.

The independent-reviewer scans the touched scope for artefacts
that qualify as both:
- **Non-critical to active product operation:** not required by
  production/runtime execution, a public interface, schema, active
  configuration, deployment process, compliance obligation, documented
  product contract, active exhaustive test store, CI/verification
  process, or current build workflow.
- **A development or testing byproduct:** created for investigation,
  debugging, experimentation, temporary scaffolding, generated
  intermediate output, one-off validation, failed/provisional work,
  or similar development/testing activity.

For each qualifying candidate, the independent-reviewer records:
- current path and product/module
- artefact type and origin (former purpose)
- evidence that it is not required by active product/build/test
  operation
- proposed archival destination
- references that must be preserved, redirected, or removed before
  relocation

The default proposed action is relocation to an archival folder, not
deletion. An uncertain candidate is marked **retain pending
verification** — uncertainty is not permission to archive.

**Output:** Archival-candidate list with disposition proposals.

Dispatch protocol:
- One Kanban card to independent-reviewer, associated with the
  parent initiative.
- The card identifies the touched-scope file list (from DEV3), the
  segment boundary, and the current repository state (commit SHA in
  the segment workspace).
- The card states what the independent-reviewer must produce: an
  archival-candidate list with disposition proposals.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV4.1b; terminal status alone is insufficient.
- Capture the archival-candidate list for DEV4.1b.

## DEV4.1b — Archival Verification

The orchestrator dispatches the archival-candidate list to the
test-authority-reviewer for independent verification against the
current
repository state.

The test-authority-reviewer checks the proposed disposition for:
- **Overreach:** a proposed item is still referenced or required by
  active product, build, test, or operational code.
- **Underreach:** a genuine orphan or superseded item was missed,
  including when no archival candidate was proposed.

The test-authority-reviewer does not move, delete, or archive
anything in this
step. It verifies and reports.

**Output:** Verified archival disposition with overreach/underreach
findings.

Dispatch protocol:
- One Kanban card to test-authority-reviewer, associated with the
  parent
  initiative.
- The card identifies the archival-candidate list from DEV4.1a and
  the current repository state (commit SHA in the segment workspace).
- The card states what the test-authority-reviewer must produce: a
  verified
  archival disposition with overreach/underreach findings.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV4.1c; terminal status alone is insufficient.
- Capture the verified disposition for DEV4.1c.

## DEV4.1c — Archival Disposition Execution

The orchestrator dispatches the verified archival disposition to the
builder-tester for execution.

The builder-tester applies only the reviewed and approved archival
disposition. It relocates approved items to their archival destination,
preserves or redirects required references, and removes references that
must go. It does not archive items that were not in the approved
disposition.

**Output:** Executed archival disposition record.

Dispatch protocol:
- One Kanban card to builder-tester, associated with the parent
  initiative.
- The card identifies the verified archival disposition from DEV4.1b
  and the segment workspace (commit SHA).
- The card states what the builder-tester must produce: an executed
  archival disposition record.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV4.2a; terminal status alone is insufficient.
- Capture the executed archival record for the DEV4 exit gate.

## DEV4.2a — Implementation-to-Design Ratification Package

The orchestrator dispatches the DEV3 build, test, and review evidence
to the independent-reviewer for ratification package authoring.

The independent-reviewer identifies each implementation-to-design
observation:
- a design requirement realised differently,
- a design element not built,
- a discovered constraint that changes the viable design shape, or
- a planned capability that should move to a future upgrade/stage.

For each observation, the package links:
- the original design source and statement
- the implemented behaviour or omitted capability
- relevant DEV3 build, test, and review evidence
- impact and rationale
- one recommended disposition: **Reflect**, **Drop**, **Defer**, or
  **Reject**

**Output:** Focused ratification package per observation.

The package is recorded on the Kanban initiative card as an initiative
comment.

Dispatch protocol:
- One Kanban card to independent-reviewer, associated with the
  parent initiative.
- The card identifies the DEV3 evidence (build record, test
  execution record, review records) from the segment workspace,
  the verified design baseline (path + commit SHA recorded at D4),
  and the ratified implementation brief (path + commit SHA from
  DEV2.3).
- The card states what the independent-reviewer must produce: a
  focused ratification package with one recommended disposition
  per observation.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV4.2b; terminal status alone is insufficient.
- Capture the ratification package for DEV4.2b.

## DEV4.2b — Focused Design-Integration Review

The orchestrator performs the focused design-integration review
directly. No dispatch card is created for this step.

The orchestrator reviews the ratification package from DEV4.2a for
material integration consequences beyond the segment: does the
proposed disposition affect other segments, other modules, the
canonical design, or the exhaustive test store? The orchestrator
also prepares the presentation for human ratification, including
the independent-reviewer's recommended dispositions and the
integration consequences of each.

**Output:** Focused integration review record and ratification
presentation.

The integration review record and presentation are recorded on the
Kanban initiative card as an initiative comment.

## DEV4.2c — Human Design Ratification

Adrian ratifies each implementation-to-design observation. This step
is non-delegable — the orchestrator does not ratify on Adrian's
behalf.

The orchestrator presents each observation with its recommended
disposition and the integration review record from DEV4.2b. Adrian
selects one outcome per observation:

- **Reflect:** the verified implemented behaviour becomes accepted
  design intent.
- **Drop:** the design element is explicitly abandoned, with approved
  rationale.
- **Defer:** the design element is removed from the current commitment
  and migrated to a named future upgrade or stage.
- **Reject:** the implementation divergence is not ratified; the
  affected work returns to DEV3 for correction against the existing
  ratified design.

**Output:** Human ratification record per observation.

The ratification record is recorded on the Kanban initiative card as
an initiative comment.

## DEV4.2d — Write-Gated Integration or DEV3 Return

For each ratification outcome:

- **Reflect / Drop / Defer:** The orchestrator routes the ratified
  outcome through the D1-D4 Write-Gated integration path. D1 scopes
  the design change, D2 reviews it, D3 ratifies it, and D4 integrates
  it into the canonical design under Write-Gate control. The
  orchestrator tracks the D1-D4 chain to completion before the DEV4
  exit gate can be satisfied.
- **Reject:** The orchestrator returns the affected work to DEV3 for
  correction against the existing ratified design. The DEV3 exit gate
  must be re-satisfied before DEV4 can proceed.

**Output:** Completed D1-D4 integration record (Reflect/Drop/Defer)
or DEV3 return record (Reject).

## DEV4.2e — Implementation-Context Capture

The orchestrator records ordinary implementation-specific learning in
durable implementation context. This is not promotion to canonical
design — it is a note for future sessions and segments.

The orchestrator confirms that no implementation-to-design observation
has been left only in implementation history without a recorded
ratification outcome or an explicit DEV3 correction route.

**Profile documentation.** Based on the segment's task documentation,
the orchestrator identifies which skills or memories each profile
should carry to avoid repeat mistakes or reduce thrashing. If
improvements are identified, the orchestrator implements the profile
memory or skill update immediately in this step. The orchestrator
identifies:
- which profile encountered which recurring obstacle or ambiguity,
- whether a skill or memory entry would prevent the repeat,
- and whether the existing skill/memory adequately covered the
  case or needs a targeted update.

The orchestrator applies any identified skill or memory updates
during DEV4.2e. These updates are profile-level changes (skill
content or memory entries) that persist across segments and are
available to the profile in subsequent work.

**Segment documentation file.** The orchestrator creates a
segment documentation file at
`2-design/<segment-id>/segment-doc.md` (or the location declared
by the segment manifest). This file records:
- segment identity and boundary
- implementation lessons learned during DEV3 and DEV4
- profile-specific observations (what each profile struggled
  with, what was non-obvious, what required rework)
- skill and memory update recommendations for the next segment
- any deviations from the ratified implementation brief and
  their disposition
- archival disposition summary from DEV4.1
- ratification outcomes from DEV4.2

The file is committed to the segment workspace before the merge in
DEV4.3. It is the durable record of this segment's implementation
experience, available to cold-session agents in subsequent
segments or initiatives. Profile-level skill and memory updates
identified in this step are applied directly and are not deferred
to the documentation file.

**Output:** Updated implementation-context record and committed
segment documentation file.

## DEV4.3 — Segment Merge and Integration

DEV4.3 is coordinator-owned. Paths and delivery heads are not
caller-selected. The orchestrator verifies the accepted DEV3 heads,
seals each current delivery head, performs the ancestry-preserving
merges into `origin/main`, and records the accepted merge checkpoint.

Prerequisites before merge:
- The DEV4.1 archival disposition is executed.
- All DEV4.2 observations have a recorded ratification outcome
  (Reflect/Drop/Defer/D1-D4 completed, or Reject/DEV3 return
  re-satisfied).
- The segment workspace is on a clean commit that includes all
  DEV4 evidence.

The orchestrator:
1. Verifies the accepted DEV3 heads and seals each current delivery
   head.
2. Merges the sealed segment branch into `origin/main` with
   ancestry preserved.
3. Verifies the merge: the merged SHA is recorded, the merge
   introduces no new test failures against the exhaustive test store,
   and the segment boundary is preserved in the merged state.
4. Records the accepted merge checkpoint (branch, source SHA, target
   SHA, merge commit SHA) under `2-design/<segment-id>/`.

A failed Kanban transition after a successful Git merge is a Kanban
mechanics failure. The merge evidence remains valid; the Kanban
mechanics are repaired and the transition is retried. The merge itself
is not invalidated.

**Output:** Merge evidence record.

## DEV4.4 — Workspace Retirement

DEV4.4 is coordinator-owned retirement. The orchestrator verifies the
merged state, marks every member and the workspace retired/inactive,
and records the accepted retirement checkpoint. The worktree is not
deleted merely as an agent instruction.

After the merge is verified and the Kanban transition is recorded, the
segment workspace is no longer the writable authority. The
orchestrator:
1. Verifies the merged state and confirms no pending writes remain in
   the segment workspace.
2. Marks every member and the workspace retired/inactive, recording the
   retirement in the segment register at `2-design/segment-register.md`
   (state updated to retired).
3. Records the accepted retirement checkpoint and releases the
   `segment_workspace_id` binding on the task.

The retired workspace is not deleted. It remains as a reference until
the repository lifecycle determines its final disposition.

**Output:** Accepted retirement checkpoint.

## DEV4.5 — Next-Segment Admission

DEV4.5 cites the accepted DEV4.4 retirement checkpoint through its exact
`predecessor_ref` and records either the next-segment DEV2 route or the
final initiative closure route.

The orchestrator determines the next action based on the segment
register:

- **If more segments remain in the initiative:** The next segment is
  admitted directly from `DEV4/Sx → DEV2/Sx+1`. The DEV1.7 combined
  listing is not re-run. The next segment's worktree is materialized
  from the updated `origin/main` (which now includes this segment's
  merge). The orchestrator performs the controlled transition and
  dispatches DEV2 for the next segment.
- **If no more segments remain:** The initiative is closed. The public
  close operation requires the final accepted DEV4.5 checkpoint and the
  exact approved closure evidence. The orchestrator records the closure
  on the Kanban initiative card.

**Output:** Accepted DEV4.5 checkpoint recording the next-segment
transition or the initiative closure.

## Records

- Archival-candidate list: committed under `2-design/<segment-id>/`.
- Verified archival disposition: committed under
  `2-design/<segment-id>/`.
- Executed archival record: committed under
  `2-design/<segment-id>/`.
- Ratification package: recorded as an initiative comment on the
  Kanban card.
- Integration review record: committed under
  `2-design/<segment-id>/`.
- Ratification record: recorded as an initiative comment on the
  Kanban card.
- D1-D4 integration record: committed under the canonical design
  path, cross-referenced from `2-design/<segment-id>/`.
- Implementation-context record: committed under
  `2-design/<segment-id>/`.
- Segment documentation file: committed under
  `2-design/<segment-id>/segment-doc.md` (or the manifest-declared
  location).
- Merge evidence: committed under `2-design/<segment-id>/`.
- Workspace retirement record: recorded in the segment register at
  `2-design/segment-register.md`.
- Kanban: each DEV4 dispatch (DEV4.1a, DEV4.1b, DEV4.1c, DEV4.2a)
  creates a child card linked to the initiative. DEV4.2b, DEV4.2c,
  DEV4.2d, DEV4.2e, DEV4.3, DEV4.4, and DEV4.5 are orchestrator
  actions recorded on the initiative card. DEV4.3 and DEV4.4 are
  coordinator-owned; their accepted checkpoints are cited by later tasks
  through the exact `predecessor_ref`.

## Phase outcome and route

DEV4 is complete when:
- The archival disposition has been scanned (DEV4.1a), verified
  (DEV4.1b), and executed (DEV4.1c).
- Every implementation-to-design observation has a recorded
  ratification outcome: D1-D4 integration completed (Reflect/Drop/
  Defer) or DEV3 return re-satisfied (Reject).
- Ordinary implementation learning is captured in implementation
  context, and the segment documentation file is committed.
- The segment has been merged into `origin/main` with verified merge
  evidence.
- The segment workspace has been retired and the binding released.
- The next segment has been admitted (DEV4.5) or the initiative has
  been closed.
- The Kanban initiative card is updated with the DEV4 closing result
  and the controlled transition is recorded.

The segment is closed. If more segments remain, the initiative
proceeds to DEV2 for the next segment. If no segments remain, the
initiative is closed and PC1 may be triggered at milestone closure.

## Failure modes

- Scanner claim as authority: An item is archived based only on the
  archival-candidate scanner's claim without independent verification.
  DEV4.1b must verify the disposition before DEV4.1c executes it.
- Empty candidate list as proof: No archival candidate is proposed, so
  it is assumed none exists. Underreach must be checked explicitly.
- Active code in archival list: An item required by active product,
  build, test, or operational code appears in the archival-candidate
  list. It must not be included.
- Uncertainty as permission: An uncertain candidate is archived rather
  than marked retain pending verification. Uncertainty blocks archival.
- Silent design promotion: An implementation detail is promoted to
  canonical design without a ratified D1-D4 outcome. DEV4.2 must not
  silently change the design.
- Relitigation in DEV4.2: DEV4.2 is used to re-review the complete
  design. Its scope is the identified implementation-to-design
  observation and its material integration consequences only.
- Merge without clean state: The segment is merged with uncommitted
  evidence or an unclean state. The merge must be from a clean commit
  that includes all DEV4 evidence.
- Kanban transition as merge authority: A failed Kanban transition is
  treated as a failed merge. The Git merge and its evidence are
  independent of the Kanban transition; a Kanban mechanics failure
  does not invalidate the merge.

## Authority

- Canon/design-lifecycle.md section DEV4 — Closure and Capture
  procedure, archival rules, implementation-to-design ratification,
  boundaries, exit gate, and route.
- Canon/design-lifecycle.md Core Rule 4 — independent review
  requirement (applies to DEV4.1a, DEV4.1b, DEV4.2a, and DEV4.2b
  dispatches).
- Canon/design-lifecycle.md Core Rule 5 — design gap routing
  (applies to DEV4.2 Reject outcomes returning to DEV3).
- Canon/design-lifecycle.md Core Rule 7 — lifecycle advancement
  discipline (applies to the DEV4 exit gate and next-segment
  admission).
- The verified design baseline (path + commit SHA recorded at D4).
- The ratified implementation brief (path + commit SHA from
  DEV2.3).
- The segment register at `2-design/segment-register.md` — the
  read-only reference for segment identity, boundary, dependencies,
  dispatch package reference, current state, and retirement.
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
  closed rather than being overwritten. Paths and delivery heads are not
  caller-selected.
- The Kanban initiative card — the single reference point for the
  initiative's lifecycle state and DEV4 closing result.
