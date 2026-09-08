---
name: dev3-orchestration
description: Operational guidance for DEV3 build, test, and review.
version: 0.1.0
author: Adrian Lin, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    kanban_phase: DEV3
    kanban_contract_id: adrian-kanban.lifecycle.dev3
    kanban_contract_version: "1"
    tags: [lifecycle, dev3, build, test, review, orchestration]
---

# DEV3 — Build, Test, Independent Review, and Remediation

DEV3 runs after DEV2. It builds the functionality defined by the ratified
implementation brief, tests and reviews it from independent viewpoints,
classifies every finding, and repeats the cycle after every applied
correction until the segment is clear or returned to the design
lifecycle.

## Your role

The orchestrator coordinates DEV3. The builder-tester builds and drafts
tests. The test-authority-reviewer exercises test authority and broad
code check. The independent-reviewer provides independent review and
synthesis. The orchestrator classifies findings and drives the
remediation loop.

Role assignments:
- DEV3.1: builder-tester — building
- DEV3.2: builder-tester — candidate test and fixture drafting
- DEV3.3: test-authority-reviewer — Design to Tests Check
- DEV3.4: test-authority-reviewer — test execution
- DEV3.5: test-authority-reviewer — Design and Brief to Code Check
- DEV3.6: independent-reviewer — Design and Tests to Test-Results Check
- DEV3.7: independent-reviewer — Independent Design and Brief to Code Check
- DEV3.8: independent-reviewer — code-review synthesis
- DEV3.9: orchestrator — finding classification
- DEV3.10: builder-tester — revision brief authoring
- DEV3.11: builder-tester — revision application
- DEV3.12: all profiles — revision recheck through full-cycle repeat

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
6. Resolve the segment workspace from the task card's
   `segment_workspace_ref`.
   <!-- TODO: deterministic workspace resolution logic — to be provided
   when the Kanban workspace resolver is implemented. -->
   Verify the worktree exists, is on the expected branch, and its base
   SHA matches the segment register's recorded base SHA.
7. Obtain the trusted workspace binding
   (CONFIRMED_WORKTREE_BINDING v1). Confirm the binding's
   `segment_id`, `worktree_path`, and `git_branch` match the resolved
   segment workspace.
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
- The task card's `segment_id` does not match the active segment from
  the chain.
- The task card's `segment_workspace_ref` is missing or does not match
  the expected path.
- The worktree does not exist, is on the wrong branch, or its base SHA
  does not match.
- The workspace binding is missing or does not match the segment
  workspace.
- The segment's dependencies are not all closed.

### Phase-specific continuation (DEV3)

- Work only from this segment's accepted implementation brief (the
  DEV2 output at `2-design/<segment-id>/`).
- Implement within the segment's declared boundary. Do not extend the
  segment, invent missing rules, or work on other segments.
- The segment workspace is the writable workspace for all
  implementation, testing, and review work.
- Commit build evidence, test results, and review records to
  `2-design/<segment-id>/` (Git).
- The worktree remains in the segment workspace throughout DEV3. Do
  not create a separate worktree for DEV3.

## DEV3.1 — Building

The orchestrator dispatches the ratified implementation brief to the
builder-tester profile for building.

The builder-tester builds the functionality described by the ratified
implementation brief. The builder-tester may make normal implementation
choices required to realise that functionality, but must not change
intended behaviour, extend the segment, invent a missing rule, or
resolve a design ambiguity in code.

The builder-tester records changed and read files, assumptions,
verification attempted, exact results, and unresolved questions.

**Output:** Build artefacts and build summary.

Dispatch protocol:
- One Kanban card to builder-tester, associated with the parent
  initiative.
- The card identifies the ratified implementation brief (path +
  commit SHA from DEV2.3) and the verified design baseline (path +
  commit SHA recorded at D4).
  <!-- TODO: exact card fields and workspace binding to be confirmed
  once the Kanban field contract is finalized. -->
- The card states what the builder-tester must produce: build
  artefacts and a build summary.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV3.2; terminal status alone is insufficient.
- Capture the build summary and artefact references for DEV3.2.

## DEV3.2 — Candidate Test and Fixture Drafting

The orchestrator dispatches the build summary and artefacts to the
builder-tester profile for candidate test and fixture drafting.

The builder-tester drafts candidate happy-, unhappy-, and fringe-path
tests and their required fixtures directly from the verified design
baseline. Each test suite (happy, unhappy, fringe) is generated in a
separate dispatch — the builder-tester does not produce all three in
one issuance. Each candidate must identify its design oracle, path
type, behaviour under test, fixture required, expected assertion, and
out-of-scope boundary.

The builder-tester's candidate tests and fixtures are untrusted
material; they are not accepted test authority.

**Output:** Candidate tests and fixtures with declaration records.

Dispatch protocol:
- One Kanban card to builder-tester, associated with the parent
  initiative.
- The card identifies the build summary from DEV3.1, the verified
  design baseline (path + commit SHA recorded at D4), and the ratified
  implementation brief.
- The card states what the builder-tester must produce: candidate
  tests and fixtures with declaration records.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV3.3; terminal status alone is insufficient.
- Capture the candidate tests and fixtures for DEV3.3.

## DEV3.3 — Design to Tests Check

The orchestrator dispatches the candidate tests and fixtures to the
test-authority-reviewer profile for an independent Design to Tests
Check.

The test-authority-reviewer independently derives required behaviour
and coverage from the verified design baseline, contextualizing the
review across happy-path, unhappy-path, and fringe-path behaviour.
The
test-authority-reviewer then ratifies, rewrites, or rejects the
builder-tester's candidate tests and fixtures. The
test-authority-reviewer must not merely confirm that the candidate
tests run.

Every test-authority-reviewer-ratified or test-authority-reviewer-rewritten
test and its required fixture is stored in the **exhaustive test store**
for the relevant product/module. The store is distinguished by product
and module. Each stored test retains its design oracle, path type,
behaviour under test, fixture requirement, expected assertion,
out-of-scope boundary, and ratification record. Builder-tester's
rejected or still-unratified candidate material must not enter the
exhaustive test store.

**Output:** Updated product/module exhaustive test store,
test-authority-reviewer-ratified test suite, and test-coverage
conclusion.

Dispatch protocol:
- One Kanban card to test-authority-reviewer, associated with the
  parent initiative.
- The card identifies the candidate tests and fixtures from DEV3.2,
  the verified design baseline (path + commit SHA recorded at D4),
  and the build summary from DEV3.1.
  <!-- TODO: exact card fields and workspace binding to be confirmed
  once the Kanban field contract is finalized. -->
- The card states what the test-authority-reviewer must produce: an
  updated exhaustive test store, a ratified test suite, and a
  test-coverage conclusion.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV3.4; terminal status alone is insufficient.
- Capture the ratified test suite and test-coverage conclusion for
  DEV3.4.

## DEV3.4 — Test Execution

The orchestrator dispatches the ratified test suite to the
test-authority-reviewer profile for test execution.

The test-authority-reviewer executes the complete contents of the
exhaustive test store for the relevant product/module against the
current build — not only the tests created or changed for the current
segment. The test-authority-reviewer records the product/module
test-store identity, every stored test selected for execution, complete
execution evidence, results, failure interpretation, and coverage
conclusion.

Test failures and bugs identified during execution are resolved within
DEV3 before the segment proceeds. A failing test store is not a
carryover to DEV4.

**Output:** Complete product/module test-store execution record.

Dispatch protocol:
- One Kanban card to test-authority-reviewer, associated with the
  parent initiative.
- The card identifies the exhaustive test store (product/module
  identity), the current build state (commit SHA in the segment
  workspace), and the verified design baseline.
  <!-- TODO: exact card fields and workspace binding to be confirmed
  once the Kanban field contract is finalized. -->
- The card states what the test-authority-reviewer must produce: a
  complete test-store execution record.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV3.5; terminal status alone is insufficient.
- Capture the execution record for DEV3.6.

## DEV3.5 — Design and Brief to Code Check

The orchestrator dispatches the build to the test-authority-reviewer
profile for the broad code check.

The test-authority-reviewer conducts the broad code check against the
verified design baseline and ratified implementation brief. The
review includes adherence to the naming conventions defined in the
DEV1.6 products.

**Output:** test-authority-reviewer broad code-review findings.

Dispatch protocol:
- One Kanban card to test-authority-reviewer, associated with the
  parent initiative.
- The card identifies the current build state (commit SHA in the
  segment workspace), the verified design baseline (path + commit SHA
  recorded at D4), and the ratified implementation brief (path +
  commit SHA from DEV2.3).
- The card states what the test-authority-reviewer must produce: broad
  code-review findings.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV3.6; terminal status alone is insufficient.
- Capture the broad code-review findings for DEV3.8 synthesis.
  The test-authority-reviewer's findings are NOT made available to
  the independent-reviewer until DEV3.7 is recorded.

## DEV3.6 — Design and Tests to Test-Results Check

The orchestrator dispatches the test execution record to the
independent-reviewer profile for an independent test-result review.

The independent-reviewer independently reviews the
test-authority-reviewer's ratified tests and complete execution
evidence against the verified design baseline. The independent-reviewer
verifies that the executed tests match their declared behaviour and
assertions, that result interpretation and failure classification are
supported, and that required design behaviour has not been omitted from
the coverage conclusion.

Passing tests alone are not proof that the implementation satisfies the
design.

**Output:** independent-reviewer test-result review record.

Dispatch protocol:
- One Kanban card to independent-reviewer, associated with the parent
  initiative.
- The card identifies the exhaustive test store identity, the complete
  execution record from DEV3.4, and the verified design baseline (path
  + commit SHA recorded at D4).
  <!-- TODO: exact card fields and workspace binding to be confirmed
  once the Kanban field contract is finalized. -->
- The card states what the independent-reviewer must produce: a
  test-result review record.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV3.7; terminal status alone is insufficient.
- Capture the test-result review record for DEV3.8 synthesis.

## DEV3.7 — Independent Design and Brief to Code Check

The orchestrator dispatches the build to the independent-reviewer
profile for an independent code review.

The independent-reviewer reads the verified design baseline, ratified
implementation brief, and changed code directly. The independent-reviewer
forms and records an independent code-review finding set before reading
the test-authority-reviewer's broad code-review findings.

**Output:** independent-reviewer independent code-review record.

Dispatch protocol:
- One Kanban card to independent-reviewer, associated with the parent
  initiative.
- The card identifies the current build state (commit SHA in the
  segment workspace), the verified design baseline (path + commit SHA
  recorded at D4), and the ratified implementation brief (path +
  commit SHA from DEV2.3).
  <!-- TODO: exact card fields and workspace binding to be confirmed
  once the Kanban field contract is finalized. -->
- The card states what the independent-reviewer must produce: an
  independent code-review record.
- The card must state that the independent-reviewer must NOT read
  the test-authority-reviewer's broad code-review findings (from
  DEV3.5) before recording the independent finding set.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV3.8; terminal status alone is insufficient.
- Capture the independent code-review record for DEV3.8 synthesis.

## DEV3.8 — Code-Review Synthesis

The orchestrator dispatches both code-review records to the
independent-reviewer profile for synthesis.

Only after DEV3.7 is recorded, the independent-reviewer reads the
test-authority-reviewer's broad code-review findings. The
independent-reviewer synthesizes both code-review viewpoints and records
agreements, disagreements, and gaps missed by either review.

Code-review findings are resolved within DEV3 before the segment
proceeds. Unresolved review findings are not a carryover to DEV4.

**Output:** Integrated review record and agreement, disagreement, and
gap register.

Dispatch protocol:
- One Kanban card to independent-reviewer, associated with the parent
  initiative.
- The card identifies the independent code-review record from DEV3.7
  and the test-authority-reviewer's broad code-review findings from
  DEV3.5.
  <!-- TODO: exact card fields and workspace binding to be confirmed
  once the Kanban field contract is finalized. -->
- The card states what the independent-reviewer must produce: an
  integrated review record with agreement, disagreement, and gap
  register.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV3.9; terminal status alone is insufficient.
- Capture the integrated review record for DEV3.9 classification.

## DEV3.9 — Finding Classification

The orchestrator performs the finding classification directly. No
dispatch card is created for this step.

The orchestrator classifies every finding from the
test-authority-reviewer (DEV3.3, DEV3.4, DEV3.5) and the
independent-reviewer (DEV3.6, DEV3.7, DEV3.8) before any correction is
written or applied. Each classification must identify the supporting
evidence and route:

| Classification | Required route |
|---|---|
| Build bug or test defect | Remediate within DEV3 through
|   DEV3.10-DEV3.12, then repeat the full DEV3 cycle. |
| Design gap | Return to D1 for design revision, then complete
|   D4 before affected implementation resumes. |
| Design ambiguity | Stop for an explicit human decision; do not
|   guess. |
| Implementation detail | Record in durable implementation context;
|   no correction is required unless it constitutes a defect. |
| Design-relevant decision | Return to D1-D4 for Write-Gated
|   design integration. |

**Output:** Classification record, committed to main under
`2-design/<segment-id>/`.

The classification record is recorded on the Kanban initiative card as
an initiative comment.

## DEV3.10 — Revision Brief Authoring

For a remediable implementation finding, the orchestrator dispatches a
revision brief authoring task to the builder-tester profile.

The builder-tester authors a bounded correction brief containing
target surface, exact defect, acceptance condition, cited governing
sources, constraints, and non-goals. The revision brief must preserve
the original brief's intent and source grounding while incorporating the
classified finding.

**Output:** Bounded revision brief.

Dispatch protocol:
- One Kanban card to builder-tester, associated with the parent
  initiative.
- The card identifies the classification record from DEV3.9 (specific
  finding), the ratified implementation brief, and the verified design
  baseline.
- The card states what the builder-tester must produce: a bounded
  revision brief.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV3.11; terminal status alone is insufficient.
- Capture the revision brief for DEV3.11.

## DEV3.11 — Revision Application

The orchestrator dispatches the revision brief to the builder-tester
profile for application.

The builder-tester applies only the bounded revision brief. The
builder-tester records the changed surface and implementation
evidence.

**Output:** Revised build artefacts and revision evidence.

Dispatch protocol:
- One Kanban card to builder-tester, associated with the parent
  initiative.
- The card identifies the bounded revision brief from DEV3.10 and the
  current build state (commit SHA in the segment workspace).
- The card states what the builder-tester must produce: revised build
  artefacts and revision evidence.
- Wait for the contract's accepted completion or checkpoint before
  proceeding to DEV3.12; terminal status alone is insufficient.
- Capture the revision evidence for DEV3.12.

## DEV3.12 — Revision Recheck Through Full-Cycle Repeat

A revision is not accepted through a narrow local recheck. After
DEV3.11, the revised build must return to DEV3.1 and repeat the full
DEV3.1-DEV3.9 cycle:

1. builder-tester rebuilds against the ratified implementation brief
   plus the bounded revision brief and regenerates the candidate test
   and fixture material against the verified design baseline.
2. test-authority-reviewer repeats Design to Tests Check, executes the
   ratified tests, and repeats Design and Brief to Code Check.
3. independent-reviewer repeats the independent test-results check,
   independent code check, and code-review synthesis.
4. The orchestrator repeats finding classification.

This full-cycle requirement prevents a locally applied correction from
losing the broader build context or introducing a new defect through
changes made by local LLMs.

**Output:** Full-cycle revision-recheck record.

The orchestrator drives the repeat cycle by re-dispatching each step
in sequence, exactly as in the initial cycle. Each re-dispatch carries
the prior cycle's relevant records as context.

## Records

- Build artefacts and build summary: committed to main under
  `2-design/<segment-id>/`.
- Candidate tests and fixtures: committed to main under
  `2-design/<segment-id>/`.
- Exhaustive test store: committed to main under the product/module
  test-store location. <!-- TODO: exact test-store location to be
  confirmed once the Kanban field contract is finalized. -->
- Test execution record: committed to main under
  `2-design/<segment-id>/`.
- Broad code-review findings: committed to main under
  `2-design/<segment-id>/`.
- Test-result review record: committed to main under
  `2-design/<segment-id>/`.
- Independent code-review record: committed to main under
  `2-design/<segment-id>/`.
- Integrated review record: committed to main under
  `2-design/<segment-id>/`.
- Classification record: committed to main under
  `2-design/<segment-id>/` and recorded as an initiative comment.
- Revision brief and revision evidence: committed to main under
  `2-design/<segment-id>/`.
- Kanban: each DEV3 dispatch creates a child card linked to the
  initiative. DEV3.9 (classification) is an orchestrator action
  recorded on the initiative card, not a separate dispatch card.
  <!-- TODO: exact Kanban card fields, transition record schema,
  and workspace binding details to be confirmed once the Kanban
  field contract is finalized. -->

## Phase outcome and route

DEV3 is complete when:
- The build has been completed by the builder-tester (DEV3.1).
- Candidate tests and fixtures have been drafted by the builder-tester
  (DEV3.2).
- The Design to Tests Check has been completed by the
  test-authority-reviewer (DEV3.3).
- The test execution has been completed by the
  test-authority-reviewer (DEV3.4).
- The broad code check has been completed by the
  test-authority-reviewer (DEV3.5).
- The test-result review has been completed by the
  independent-reviewer (DEV3.6).
- The independent code check has been completed by the
  independent-reviewer (DEV3.7).
- The code-review synthesis has been completed by the
  independent-reviewer (DEV3.8).
- All findings have been classified by the orchestrator (DEV3.9).
- No unresolved remediable implementation finding remains.
- Any design issue has been routed to D1-D4 or stopped for explicit
  human decision.
- The classification record is committed to main.
- The Kanban initiative card is updated with the DEV3 closing result.

The segment proceeds to DEV4 when the DEV3 exit gate is satisfied.

## Failure modes

- Builder self-certification: The builder-tester certifies its own
  build or candidate tests. The build and tests are untrusted until
  independently reviewed by the test-authority-reviewer and the
  independent-reviewer.
- Candidate tests as authority: The builder-tester's candidate tests
  are treated as accepted test authority. They are untrusted material
  until ratified or rewritten by the test-authority-reviewer in
  DEV3.3.
- Narrow recheck after correction: A revision is accepted through a
  local recheck of only the changed code. The full DEV3.1-DEV3.9 cycle
  must repeat after every applied correction.
- Independent review contaminated: The independent-reviewer reads the
  test-authority-reviewer's findings before recording its own
  independent view. The independence boundary is compromised.
- Passing tests as proof: Tests pass, so the implementation is
  assumed correct. Passing tests alone are not proof that the
  implementation satisfies the design.
- Design gap settled in code: A design gap or design ambiguity is
  resolved in the build rather than routed to D1-D4 or stopped for
  human decision. DEV3 must not invent design authority.
- Segment boundary drift: The build extends beyond the segment's
  declared boundary. The build must stay inside the ratified segment
  boundary from DEV1.

## Authority

- Canon/design-lifecycle.md section DEV3 — Build, Test, Independent
  Review, and Remediation procedure, finding routes, boundaries, exit
  gate, and route.
- Canon/design-lifecycle.md Core Rule 4 — independent review
  requirement (applies to DEV3.3-DEV3.8 dispatches).
- Canon/design-lifecycle.md Core Rule 5 — design gap routing
  (applies to DEV3.9 finding routes).
- Canon/design-lifecycle.md Core Rule 7 — lifecycle advancement
  discipline (applies to the DEV3 exit gate and the DEV4 handoff).
- The verified design baseline (path + commit SHA recorded at D4).
- The ratified implementation brief (path + commit SHA from
  DEV2.3).
- The segment register at `2-design/segment-register.md` — the
  read-only reference for segment identity, boundary, dependencies,
  dispatch package reference, and current state.
- The task card's `segment_workspace_ref` — the source of truth for
  the segment worktree path.
  <!-- TODO: deterministic workspace resolution, multi-repository
  binding behavior, and single-writer constraint details to be
  confirmed once the Kanban field contract is finalized. -->
- The Kanban initiative card — the single reference point for the
  initiative's lifecycle state and DEV3 closing result.
