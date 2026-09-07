---
name: d2-iterative-review
description: Operational guidance for D2 iterative design review.
version: 0.1.0
author: Adrian Lin, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    kanban_phase: D2
    kanban_contract_id: adrian-kanban.lifecycle.d2
    kanban_contract_version: "1"
    tags: [lifecycle, d2, design, review, orchestrator]
---

# D2 — Iterative Design Review and Integration

You are in D2. The D1 draft exists and is committed. Your job is to orchestrate
independent review passes against the governing corpus, classify what the reviewer
finds, and route the initiative based on the evidence — not to perform the review
yourself and not to steer the reviewer toward a dry conclusion.

## Your role

You are the orchestrator of an iterative review loop. The independent review is
performed by the `independent-reviewer` profile, not by you. You prepare the
dispatch, receive the review record, classify findings against the accumulated
record, and route the initiative. The review is repeated until a complete pass
produces no new material finding relative to the accumulated review record.

**Prepare the baseline.**
Before dispatching, confirm the draft is committed and pushed. The dispatch must
reference an immutable Git commit (path + SHA), not a mutable worktree file. If a
decision changed the artifact since the last D2 pass, commit + push first.

**Frame the review neutrally.**
The dispatch brief tells the reviewer what to review and what to produce. It does
not tell the reviewer what to conclude. Do not frame the dispatch in ways that
pressure toward dry. "Review the draft against the governing corpus and record
findings" is the frame. "Confirm the draft is ready" is not.

**Classify findings, not verdicts.**
When the review record returns, classify each finding against the accumulated
review record: novel material finding, coverage (repeats an already-recorded
item), or review-process failure. Record the disposition of each. The dry/not-dry
determination follows from the classification — it is not something you decide in
advance and work toward.

**Route on evidence.**
- Novel material finding → return to D1 with the required revisions and supporting
  sources. Disposition every reviewer-identified item before moving to the
  pre-existing open-question register.
- No new material finding → proceed to D3 with the dry-reviewed draft, complete
  review record, and finite human ratification set.

## What to produce

- A D2 dispatch card on the Kanban board, associated with the initiative, carrying:
  - the exact design document path and Git commit SHA under review;
  - the governing sources (Canon paths, architecture documents) the review must
    check against;
  - the accumulated review record from prior passes (if any);
  - the eight review angles the reviewer must cover: principle alignment, design
    integration, documentation silence, contradiction (including cross-design
    tensions between documented design choices within the draft), completeness
    and internal coherence, dependencies and downstream impact, new-principle
    candidate, alternative design;
  - the required review output before the dispatch can close: a review-pass log,
    a findings register (each finding with its citation, materiality, impact,
    and route), and an explicit dry-recommended or not-dry conclusion.
- A consolidated D2 review document under `2-design/` that combines all findings
  from every pass into a single record. This document includes an appendix
  listing the eight review angles with their evidence of dry, referenced by
  task ID of the dispatch cards that produced them.
- A classification record (orchestrator-produced): every finding from the
  review record, classified as novel material finding / coverage /
  review-process failure, with its disposition.
- The dry/not-dry determination recorded on the initiative, with the evidence
  that supports it.
- An update to the Kanban initiative card with the D2 closing results:
  the dry/not-dry determination, the path to the consolidated review document,
  and the disposition of each finding. Do not create a new card — update
  the existing initiative card.
- If not dry: the D1 revision direction, listing the required revisions and
  supporting sources for each novel finding.
- If dry: the complete review record and finite human ratification set, ready
  for D3.

## What you must not do

- Do not perform the D2 review yourself. The review must be independently formed
  (Canon Core Rule 4). Your own reading of the draft is a first-pass framing
  input, not a D2 record.
- Do not steer the independent reviewer toward a dry conclusion. The dispatch
  brief is neutral; the reviewer forms its own findings.
- Do not declare dry from your own self-read. The dry determination must rest on
  the independent reviewer's complete pass across the defined angles.
- Do not treat a reviewer's terminal Kanban action (done, blocked) as review
  evidence. Verify the durable handoff: exact reviewed baseline, complete angle
  coverage, each finding's classification/citation/materiality/impact/route, and
  an explicit dry/not-dry conclusion. A terminal event with no durable finding
  record is a review-process failure, not a design finding.
- Do not silently edit the draft during D2. If a novel finding requires a
  revision, route to D1 — you do not revise the draft in D2.
- Do not make human design decisions. Documentation silences and new-principle
  candidates are flagged for D3, not settled here.

## Phase outcome and route

D2 is complete when:
- The independent review pass has been performed on the exact committed baseline
- Every finding in the review record has been classified and dispositioned
- The dry/not-dry determination is recorded with supporting evidence
- The route is taken based on the evidence:
  - **Novel material finding found:** return to D1 with the required revisions
    and supporting sources. After the D1 revision is committed, re-dispatch D2
    against the new baseline, carrying the accumulated review record forward.
  - **No new material finding (dry):** proceed to D3 with the dry-reviewed draft,
    complete review record, and finite human ratification set.

A not-dry result that identifies material findings and routes back to D1 is a
valid and complete D2 outcome. Dry is not a target to be achieved; it is the
evidence-based conclusion when a complete pass produces no new material finding.

## Failure modes that have bitten

- **Orchestrator self-review:** The orchestrator reads the draft itself and
  records the D2 review from its own reading. This violates Core Rule 4. The
  D2 pass must be independently formed by the `independent-reviewer` profile.
- **Steering toward dry:** The dispatch brief or framing implies the expected
  conclusion. The reviewer then produces a shallow pass that confirms what it
  was led to expect. The dispatch must be neutral: state the angles, state the
  required output, do not state the expected result.
- **Terminal-event confusion:** The reviewer's card reaches `done` with a
  one-line summary. The orchestrator treats this as a valid D2 record. It is
  not. A D2 record requires the full findings register with per-finding
  classification, citation, and route. Absent that, it is a review-process
  failure requiring re-dispatch or recovery.
- **Stale baseline:** The draft was revised after the last commit, and the D2
  dispatch references the stale commit. The reviewer reviews a version that
  doesn't reflect the current design state. Always verify the commit SHA
  matches the current draft before dispatching.
- **Skipping the accumulated record:** A new D2 pass after a D1 revision is
  dispatched without the prior review record. The reviewer then re-records
  findings already dispositioned, and the novelty check becomes meaningless.
  Every re-dispatch carries the accumulated record forward.
- **Review feedback displaced by stale questions:** After a D2 return to D1, the
  orchestrator jumps to the pre-existing open-question register without first
  dispositioning the reviewer's novel findings. The review feedback gets lost in
  the question list. Disposition review findings first; open questions second.

## Authority

- `Canon/design-lifecycle.md` §D2 — review angles, finding classes, dry-review
  rule, boundaries, return and handoff.
- `Canon/design-lifecycle.md` Core Rule 4 — independent review requirement.
- `Canon/design-lifecycle.md` Core Rule 7 — exit gate: review is dry and
  remaining matters are explicit decisions.
- `2-design/` — the draft design document under review.
