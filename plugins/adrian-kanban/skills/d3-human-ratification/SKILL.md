---
name: d3-human-ratification
description: Operational guidance for D3 human design ratification.
version: 0.1.0
author: Adrian Lin, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    kanban_phase: D3
    kanban_contract_id: adrian-kanban.lifecycle.d3
    kanban_contract_version: "1"
    tags: [lifecycle, d3, ratification, orchestrator]
---

# D3 — Human Design Ratification

You are in D3. The D2 review is dry. The exact reviewed draft version is
committed and the complete review record exists. Read the Kanban initiative
card to confirm the D1 and D2 outputs, the exact reviewed version, and the
lifecycle state. Your job is to lead the ratification phase: synthesize the
D2 findings into a format that allows for human processing of the impact,
facilitate Adrian's ratification or revision decision, and record that
decision against the exact reviewed version. D3 is a human-led phase: you
prepare and frame the decision context, you do not make the design decision.

## Your role

You are the facilitator of a human ratification decision. Adrian is the one
who ratifies or returns the design. Your job is to lead the phase: take the
accumulated D2 review record and synthesize it into a coherent decision package
that Adrian can process, present it with sufficient context for his decision,
and record the outcome against the exact reviewed version. You do not ratify
the design yourself and you do not make design decisions on Adrian's behalf.

**Check for a decision set.**
If the D2 accumulated record contains no open items requiring a human decision
(no silences, no contradictions, no new-principle candidates, no
canon-amendment candidates), the design is ratified as-is against the exact
reviewed version. Record the ratification decision with the exact commit SHA
and proceed to D4. The review of the D2 record is still required — the absence
of open items is a positive finding, not a skipped step.

**Prepare the decision package.**
Synthesize the D2 accumulated review record into a format that supports human
processing of the design's impact. This is not a mechanical list of findings;
it is an organized presentation that lets Adrian see the full picture:
- Unresolved documentation silences flagged in D2 (items where the governing
  sources are silent and a design decision is required).
- Contradictions that remain open after D2 (items where the draft and a
  governing source conflict and the resolution is a design choice). This
  includes cross-design tensions: where upholding one documented design
  choice contradicts the rationale behind another. Surface these explicitly
  as decision points for Adrian, not buried in individual finding
  descriptions.

**Present the decision context.**
For each item, present:
- The exact draft text and its location (path + section).
- The governing source it interacts with and what that source says.
- The alternatives considered and their trade-offs.
- The D2 review evidence: which pass identified it, what the reviewer said,
  and how it was classified.
- Your recommendation and rationale (if you have one). Adrian may accept or
  reject it; it is context, not a decision.

**Record the decision against the exact version.**
When Adrian ratifies or returns the design, record the decision against the
exact commit SHA of the reviewed draft. The ratification decision record must
state:
- The exact reviewed version (path + commit SHA).
- Adrian's decision per item: ratified as-is, amended in the design document,
  or canon amendment decided.
- If amended in the design document: the explicit amendment for each item.
  The amended design document is re-submitted to D2 for re-review against the
  governing corpus.
- If canon amendment decided: the explicit decision record that this item
  requires a Canon change, which is carried into D4 for execution.
- If ratified as-is: the confirmation that the exact version is approved.

**Route on the decision.**
- Adrian ratifies the exact version → proceed to D4 with the ratified design
  baseline and decision record.
- Adrian amends the design document → record the amendments, commit + push the
  amended design document, and re-dispatch D2 against the amended baseline. The
  D2 accumulated record is carried forward into the re-pass.
- Adrian decides a canon amendment is required → record the decision and carry
  it into D4 for execution. The D3 decision record is the input to D4's
  change-set compilation.

## What to produce

- A decision package presenting every item in the finite human ratification
  set, with the context described above.
- The ratification decision record: Adrian's decision against the exact reviewed
  version (path + commit SHA), with the per-item disposition (ratified,
  amended, or canon-amendment-decided).
- An update to the Kanban initiative card with the D3 closing result: the
  per-item dispositions, the commit SHA of the reviewed version, and the path
  to the decision record.
- If ratified: the design is ready for D4. The ratification decision record is
  the input to D4's change-set compilation.
- If amended in the design document: the amended design document is committed
  and re-submitted to D2. The D3 decision record documents what was amended and
  why.
- If canon amendment decided: the decision is carried into D4. The D3 decision
  record is the input to D4's change-set compilation.

## What you must not do

- Do not ratify the design yourself. Ratification is a human design decision
  (Canon Core Rule 7). Your role is to prepare the decision context and record
  the outcome, not to make the call.
- Do not approve undocumented or unreviewed design changes. Every item in the
  decision package must trace back to a D2 finding with a classification and
  the governing source it interacts with.
- Do not treat D3 ratification as authorization to mutate canonical documents.
  Ratification is a design decision; D4 is the phase that mutates Canon.
- Do not skip the decision record. A verbal "looks good" in conversation is
  not a durable ratification. The decision must be recorded against the exact
  version with the evidence that supports it.
- Do not present items without context. Adrian needs the draft text, the
  governing source, the alternatives, and the D2 evidence to make an informed
  decision. A one-line summary is insufficient decision context.

## Phase outcome and route

D3 is complete when:
- Every item in the finite human ratification set has been presented to Adrian
  with sufficient decision context
- Adrian's decision is recorded against the exact reviewed version (path +
  commit SHA)
- The Kanban initiative card is updated with the D3 closing result
- The route is taken based on the decision:
  - **Ratified as-is:** proceed to D4 with the ratified design baseline and
    decision record.
  - **Amended in the design document:** commit + push the amended design
    document, re-dispatch D2 against the new baseline with the accumulated
    record carried forward. Re-enter D3 with the new dry-reviewed version.
  - **Canon amendment decided:** carry the decision into D4 for execution.

A design-document amendment that re-enters D2 is a valid and complete D3
outcome. A canon-amendment decision that carries into D4 is a valid and
complete D3 outcome. D3 is not a gate that must be passed on the first attempt;
it is the human decision point that either ratifies the exact version, amends
the design document, or decides a Canon change.

## Failure modes that have bitten

- **Orchestrator self-ratification:** The orchestrator concludes the design is
  sound from the D2 review and records a ratification without Adrian's
  explicit decision. This violates Core Rule 7. The ratification must be
  Adrian's call, recorded against the exact version.
- **Decision context too thin:** The decision package presents items as one-line
  summaries without the draft text, governing source, alternatives, or D2
  evidence. Adrian then ratifies or returns based on incomplete information.
  Every item needs the full context to support a defensible decision.
- **Verbal ratification without record:** Adrian says "looks good" in
  conversation, and the orchestrator records a ratification without the
  decision package, the exact version reference, or the evidence. The record
  is not durable; a future session cannot verify what was decided and why.
- **Ratification treated as write authorization:** The orchestrator treats D3
  ratification as permission to edit Canon and begins making changes. D3 is a
  design decision; D4 is the integration phase. The boundary is deliberate.
- **Skipping the D2 evidence check:** The orchestrator presents items without
  verifying they trace back to a D2 finding with a classification. An item
  that is not in the review record or is misclassified cannot be ratified; it
  needs to go back to D2 or be flagged as a new D1 item.

## Authority

- `Canon/design-lifecycle.md` §D3 — human ratification procedure, boundaries,
  return and handoff, exit gate.
- `Canon/design-lifecycle.md` Core Rule 7 — ratification is a human design
  decision.
- `2-design/` — the dry-reviewed design document and the complete D2 review
  record.
- The Kanban initiative card — the single reference point for the initiative's
  lifecycle state and decision records.
