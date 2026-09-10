# Approved implementation decisions supplementing Kanban design v0.28

This record supplements the ratified baseline; it does not rewrite v0.28 or
authorize deviations from its other requirements.

## Combined initiative reopening and movement — Adrian approved, 2026-09-09

A request to move a closed initiative may prepare one exact Write-Gate request
that explicitly includes both reopening and the requested destination phase and
segment. The initial instruction is not mutation approval. The second, distinct
human interaction approves the complete combined proposal; a separate third
approval solely for reopening is not required.

Preparation preserves the closed initiative and its current position. The preview
must show the existing closure state, intended open state, destination and derived
consequences. Execution revalidates all approved inputs and applies reopening,
movement, approval consumption and the corresponding history atomically. Changed
inputs require a new preview and approval. Cancellation, rejection and expiry leave
the initiative unchanged.

This decision does not automatically reactivate retired workspaces or bypass the
mandatory repository-reconciliation and workspace requirements. Task-specific
overrides cannot reopen their parent initiative. Closing an initiative still
requires the explicit user approval prescribed by v0.28 sections 7.9 and 7.10.

The storage representation includes `source.closed_at` and
`destination.closed_at` in the canonical proposal. For reopening, the source must
match the stored closure timestamp and the destination must explicitly be null.
The same approved digest therefore binds both closure-state and phase/segment
changes. This is an internal representation of the approved behavior, not a new
user approval step.

## Shared segment-workspace root — Adrian approved, 2026-09-10

All system-managed segment workspaces reside beneath one operator-configured
absolute root shared by every initiative and trusted repository. The physical
layout is deterministic:

`<controlled_worktree_root>/<escaped initiative_id>/<escaped segment_id>/<escaped repository_identity>`

The initiative/segment directory is the logical workspace and the exact
Session Startup, task, dispatcher, and WriteGate binding. Each repository Git
worktree is a child member directory. Task IDs and caller-supplied paths do not
participate in physical path selection. DEV1's immutable segment projection
pre-assigns the workspace identity and repository-member set; the controller
derives the path and later materializes each Git worktree from the required
baseline. No separate human approval or path choice occurs per segment.

Hermes's retained `dir` workspace behavior is reused for dispatch so every task
for DEV2 through DEV4 of a segment resolves the same segment directory and does
not create or remove a per-task worktree. Before launch, the plugin verifies the
stored directory, every member's repository, branch, base containment and head,
and the absence of another active writer for the same logical workspace.

WriteGate does not exempt the configured shared root. Its ordinary exact-
binding rule naturally permits development churn inside the active segment
subtree and rejects sibling initiatives, sibling segments, and paths outside
the binding. Repository-level `Canon`, `4-artifacts`, and `5-archive` roots
remain protected within every member and require the existing bounded lease;
an identically named directory deeper in ordinary source code is not treated as
a protected repository root.

For a top-level Hermes session in DEV2 through DEV4, Session Startup resolves
the affirmed initiative's current segment and `segment_workspace_id`, derives
the same exact segment directory from trusted Kanban state and operator config,
verifies its member worktrees, moves the session there, and presents that
derived binding for the existing human confirmation. The user confirms the
derived authority; the user does not select or type a filesystem path. A
dispatched worker receives the same binding through dispatcher preassignment
and does not run the interactive Session Startup protocol.

This decision does not broaden WriteGate's assurance boundary. The pre-tool
hook remains a policy guard rather than OS-level filesystem containment; live
Hermes code outside the bound segment requires a lease through governed tools,
while comprehensive prevention of alternate write routes requires a separate
filesystem-permission or sandbox design.

## Current-work board and initiative history — Adrian approved, 2026-09-11

The nine-phase board is a compact current-work surface. Each initiative appears
in its authoritative current lifecycle phase. Of that initiative's subordinate
task cards, the board shows only open tasks whose authoritative normalized phase
matches the initiative's current phase. Closed, completed, done, archived, and
cancelled historical tasks do not occupy board columns and do not produce
missing-phase diagnostics merely because a migrated legacy card has no lifecycle
contract.

Opening an initiative card presents its durable detail view. The initiative
description, current phase and segment, transition history, and document
references recorded in its body are immediately visible. Associated historical
tasks and their stored evidence attachments are grouped under collapsed phase
chevrons. The grouping uses an authoritative task phase when available; an
explicit phase token in a legacy title may be used only as a display grouping
fallback. No fallback classification mutates or fabricates lifecycle authority.

An open task without a recognized authoritative phase remains an actionable
diagnostic. Background board refresh preserves an open initiative detail; an
intentional project-board change closes it before loading the other project.
The dashboard and installed Desktop extension must implement the same behavior
and maintain readable foreground/background contrast in light and dark themes.
