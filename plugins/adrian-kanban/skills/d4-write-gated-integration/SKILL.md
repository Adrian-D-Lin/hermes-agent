---
name: d4-write-gated-integration
description: Operational guidance for D4 Write-Gated design integration.
version: 0.1.0
author: Adrian Lin, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    kanban_phase: D4
    kanban_contract_id: adrian-kanban.lifecycle.d4
    kanban_contract_version: "1"
    tags: [lifecycle, d4, write-gate, integration, orchestrator]
---

# D4 — Write-Gated Design Integration

You are in D4. The design has been ratified at D3. Your job is to coordinate
the exact canonical-document edits required to integrate the ratified design,
obtain Write-Gate approval for the exact change set, apply only the approved
changes, and verify the result. D4 is the write-gated integration phase: the
only point in the lifecycle where canonical documents are mutated, and only on
the strength of an explicit Write-Gate approval.

## Your role

You are the orchestrator of the D4 integration sequence. The sub-steps are
performed by different profiles, and you coordinate the flow between them:

- **D4.1 — Canonical-change author:** the `independent-reviewer` profile
  identifies every canonical document affected by the ratified decision and
  prepares the exact edit set.
- **D4.2 — Canonical-change verifier:** the `test-authority-reviewer` profile
  checks the proposed edits against the ratified decision and the current
  canonical documents.
- **D4.3 — Write-Gate approval:** you present the exact change set to Adrian
  and obtain explicit Write-Gate approval for that exact change set.
- **D4.4 — Write-Gate executor:** you apply only the approved changes to the
  canonical documents.
- **D4.5 — Post-write verifier:** the `test-authority-reviewer` profile
  verifies that the resulting documents match the approved edit set and records
  the verified design baseline.

**Dispatch D4.1 to the independent-reviewer.**
The dispatch must reference:
- The exact ratified design baseline (path + commit SHA from D3).
- The ratification decision record from D3.
- The exact canonical documents that may be affected (from the ratified design's
  integration implications).
- The required output before the dispatch can close: the exact edit set for each
  affected canonical document, with the current document state cited so the
  edits can be applied deterministically.

**Dispatch D4.2 to the test-authority-reviewer.**
The dispatch must reference:
- The exact edit set from D4.1.
- The ratification decision record.
- The current canonical documents (path + commit SHA for each).
- The required output: a verification record confirming each edit is consistent
  with the ratified decision and does not add unapproved collateral changes, or
  a list of inconsistencies that must be resolved before proceeding.

**Present the change set for Write-Gate approval.**
Present the exact change set to Adrian with:
- The ratified decision record it implements.
- The D4.1 edit set for each affected document.
- The D4.2 verification record.
- The exact current state of each affected document (path + commit SHA).
- The exact proposed edits, presented as one coherent change set.

The Write-Gate approval follows the mechanics in `Canon/write-gate-policy.md`:
the approval request is folder-level (§8), creates a five-minute session-bound
lease (§9–§10), and triggers a pre-edit recovery copy in `5-archive` (§11).
Adrian's approval must be explicit for this exact change set. A general "go
ahead" without seeing the specific edits is not a Write-Gate approval.

**Apply the approved changes.**
Apply only the edits in the approved change set to the canonical documents,
within the active lease window. Do not add, drop, or modify any edit that is
not in the approved set. If the lease expires before all edits are applied,
submit a fresh Write-Gate request for the remaining work (§10).

**Dispatch D4.5 to the test-authority-reviewer.**
The dispatch must reference:
- The approved change set.
- The exact post-write state of each affected document.
- The required output: a verification record confirming the resulting documents
  match the approved edit set, plus the verified design baseline reference.

## What to produce

- The D4.1 dispatch card on the Kanban board, associated with the initiative,
  carrying the ratified baseline, decision record, and affected canonical
  documents.
- The D4.2 dispatch card on the Kanban board, carrying the D4.1 edit set,
  decision record, and current canonical documents.
- The D4.3 presentation to Adrian: the exact change set with the D4.1 edit set,
  D4.2 verification record, and current document states.
- The Write-Gate approval record: Adrian's explicit approval of the exact
  change set, recorded against the exact document versions.
- The applied changes: the canonical documents updated to match the approved
  change set.
- The D4.5 dispatch card and the post-write verification record, confirming the
  resulting documents match the approved edit set and recording the verified
  design baseline.
- An update to the Kanban initiative card with the D4 closing result: the
  verified design baseline reference and the path to the verification record.

## What you must not do

- Do not mutate canonical documents before Write-Gate approval. The edits are
  prepared and verified, but not applied, until Adrian explicitly approves the
  exact change set.
- Do not add unapproved collateral edits. Only the exact edits in the approved
  change set are applied. No "while I'm in here" fixes.
- Do not treat D3 ratification as permission to write. D3 ratifies the design;
  D4 integrates it into canonical documents. The boundary is deliberate.
- Do not skip D4.2 verification. The test-authority-reviewer must check the
  proposed edits against the ratified decision and current documents before
  the change set is presented for approval.
- Do not apply the changes from the D4.1 output without D4.2 verification and
  D4.3 approval. The sequence is D4.1 → D4.2 → D4.3 → D4.4 → D4.5.
- Do not treat a reviewer's terminal Kanban action (done, blocked) as
  verification evidence. Verify the durable handoff: exact edit set,
  per-edit verification, and explicit conclusion.

## Phase outcome and route

D4 is complete when:
- The exact edit set has been prepared (D4.1) and verified (D4.2).
- Adrian has explicitly approved the exact change set (D4.3).
- The approved changes have been applied to the canonical documents (D4.4).
- The resulting documents have been verified against the approved edit set
  (D4.5), and the verified design baseline is recorded.
- Every review line item identified across D4.1 and D4.2 has received an
  explicit determination: applied, deferred with a cited route, or rejected
  with a stated reason. No item may be silently dropped. The D4.5 verification
  record must confirm that the full item count from D4.1 + D4.2 matches the
  count of determinations in the decision record.
- The Kanban initiative card is updated with the D4 closing result.

D4 is complete when the approved changes have been applied and verified. The
resulting design baseline is available as the development oracle.

**Route:** Proceed to DEV1.

## Failure modes that have bitten

- **Writing before approval:** The orchestrator applies the canonical edits
  before Adrian has seen and approved the exact change set. D4.4 must not
  precede D4.3. The Write-Gate approval is the only authorization to write.
- **Scope creep in the edit set:** The D4.1 output includes edits beyond what
  the ratified decision requires. The D4.2 verifier must catch these, but the
  orchestrator should also check the edit set against the decision record before
  presenting it for approval.
- **Approval without the exact change set:** Adrian is asked to "approve the
  changes" without seeing the specific edits to each document. The Write-Gate
  approval must be for the exact change set, not a general authorization.
- **Skipping D4.2 verification:** The orchestrator presents the D4.1 edit set
  directly for approval without the D4.2 verification record. The
  test-authority-reviewer's check is what catches inconsistencies between the
  proposed edits and the ratified decision.
- **Post-write drift:** The canonical documents are modified after the D4.4
  application but before the D4.5 verification. The D4.5 dispatch must reference
  the exact post-write state, and the verification must confirm it matches the
  approved edit set.
- **Silent drop of a review item:** A finding or edit from D4.1 or D4.2
  disappears from the decision record without a determination. The item count
  in the D4.5 verification must match the item count in the D4.1 + D4.2 outputs.
  An item that is not applied must have an explicit deferral or rejection
  recorded, not be omitted from the final record.

## Authority

- `Canon/design-lifecycle.md` §D4 — Write-Gated integration procedure,
  boundaries, exit gate, and route.
- `Canon/write-gate-policy.md` — approval mechanics (§8 folder-level request),
  lease registry (§9), lease duration and completion (§10), pre-edit recovery
  archive (§11), and promotion rule (§15).
- `Canon/design-lifecycle.md` Core Rule 4 — independent review requirement
  (applies to D4.1 and D4.5 dispatches).
- `Canon/design-lifecycle.md` §D3 — ratification decision record (input to
  D4.1 and D4.2).
- The ratified design baseline (path + commit SHA from D3).
- The Kanban initiative card — the single reference point for the initiative's
  lifecycle state and D4 closing result.
