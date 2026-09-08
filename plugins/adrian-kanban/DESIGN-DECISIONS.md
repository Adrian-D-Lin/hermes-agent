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
