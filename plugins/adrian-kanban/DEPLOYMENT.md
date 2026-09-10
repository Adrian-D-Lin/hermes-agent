# Adrian Kanban v0.28 stopped-cutover runbook

This document is the single operational runbook for the first production
cutover of the ratified Adrian Kanban v0.28 plugin. It composes existing
capabilities (release manifest, dark-install preflight, rollback capture,
migration engine, reverse migration) and uses their actual names. It is
documentation and release packaging only: it does not mutate live production,
invent new runtime behavior, or modify code/tests. **This runbook itself does
not authorize any production change.** Every gate below must pass before the
next phase begins; on any failure, keep the system stopped and fail closed.

## Preconditions

- One immutable release package built from a recorded source commit, staged
  beside the live tree (dark stage). Native Kanban remains authoritative until
  the cutover switch.
- The matching Windows Desktop extension build for the same release identity.
- One absolute `kanban.database_path` agreed for every runtime. The current
  live system contains several native board databases; the authoritative
  source and legacy mapping are a **separate migration exercise** and are not
  chosen here. Do not infer initiatives, task relationships, or corrections
  from legacy status, assignee, names, or body text.
- The dispatcher systemd unit runs through
  `/home/progenitor/.hermes/hermes-agent/venv/bin/python` — the Python
  environment used by the live gateway and serve services. Do not use the
  distinct `.venv` as the service executable.
- A maintenance window can be opened that stops all Kanban-writing automation.

## Phase 1 — Dark stage one immutable package

1. Build one immutable release package from the recorded source commit and
   place it beside the live tree. Never copy the plugin over the live source
   tree.
2. Record the source commit and the package/Desktop digests in the release
   manifest (`build_release_manifest`).
3. Run `verify_dark_install` read-only. All findings must pass:
   `package_digest`, `desktop_digest`, `profile_release_identity`,
   `component_version_identity`, `component_database_identity`,
   `dark_authority`.
4. Gate: `plan_dark_install` returns `activation_permitted=False` and
   `mutation_authority="native"`. No live pointer is changed and no second
   writable authority is activated. Native Kanban remains authoritative.

Evidence to record: source commit, package digest, Desktop digest, manifest
digest, preflight findings.

## Phase 2 — Pin one release and one database for every runtime

The same release must serve the default, builder-tester,
independent-reviewer, and test-authority-reviewer profiles, the gateway, the
dispatcher, the private adapter, and the matching Windows Desktop extension.
Every runtime must report the same plugin/protocol/schema identity. Mixed-version
operation must fail closed.

All runtimes use one absolute `kanban.database_path`. This is one immutable
release and one absolute database path shared by every runtime.

Placeholder/value validation step: confirm the configured
`kanban.database_path` is a real, non-placeholder absolute path. If it is a
placeholder or points at one of the observed native board databases without an
explicit decision, stop. The authoritative source and legacy mapping are a
separate migration exercise; do not silently choose one of the observed
databases.

Gate: every profile and component resolves to the manifest package root and the
manifest database identity; version identity matches `versions_json` for all.

## Phase 3 — Open the maintenance window and stop everything

Open one maintenance window and stop:

- the Windows Desktop client,
- `hermes-gateway.service`,
- `hermes-serve.service`,
- the native and plugin dispatchers,
- workers, profile processes, and all Kanban-writing automation.

Verify there are no active/claimed runs and no writable Kanban connection.

```bash
systemctl --user stop hermes-gateway.service hermes-serve.service
systemctl --user is-active hermes-gateway.service hermes-serve.service  # expect "inactive"
```

Gate: stopped state confirmed; no active/claimed runs; no writable Kanban
connection.

## Phase 4 — Capture and verify the rollback set

Capture the rollback set with `capture_rollback_set`: configuration; the live
release pointer and old immutable package; the matching Desktop build; the
database plus consistent WAL and SHM state; schema/migration metadata;
counts/hashes; and the health result.

Verify the copied SQLite database opens and passes `PRAGMA integrity_check`:

```bash
sqlite3 "$ROLLBACK_DIR/database.sqlite" "PRAGMA integrity_check;"
```

Run `verify_rollback_set`; every finding must pass.

Gate: rollback set verified and the copied database passes `integrity_check`.

## Phase 5 — Migration dry-run and reconciliation on a disposable copy

Copy the database to a disposable location. Run the migration dry-run
(`dry_run`) and reconciliation (`reconcile`) against that disposable copy
first. Do not touch the live database.

Gate: dry-run and reconciliation pass on the disposable copy.

## Phase 6 — Execute the real journaled migration exactly once

While stopped, execute the real journaled migration exactly once with
`apply_forward`. It re-verifies the rollback set, re-checks the dry-run digest
inside the transaction, and journals the operation.

Gate: `apply_forward` commits and its reconciliation reports all passed.

## Phase 7 — Atomic authority switch

Atomically switch to `kanban.mutation_authority: adrian-kanban`. In the same
maintenance change:

- set native gateway dispatch (`dispatch_in_gateway`), auto-decompose
  (`auto_decompose`), and review dispatch (`review_dispatch`) to false;
- suppress native public mutation surfaces/UI/notifications;
- enable the plugin command boundary and the plugin-owned dispatcher only
  after the switch.

Do not expose native and plugin mutation together.

Gate: config parses; `kanban.mutation_authority` is `adrian-kanban`; native
dispatch flags are false; plugin boundary and dispatcher enabled.

## Phase 8 — Preserve the Session Startup / Write-Gate profile contract

Preserve Adrian's Session Startup/Write-Gate profile contract:

- The global default profile retains Session Startup.
- `builder-tester` must not load Session Startup.
- `independent-reviewer` and `test-authority-reviewer` remain without it.
- All four use the same built-in Write-Gate configuration, remove the legacy
  pre-tool hook (the legacy pre-tool hook), and load the same Adrian Kanban
  release.

Gate: profile-specific validation passes for all four profiles.

## Phase 9 — Pre-reconnect checks and canaries

Before clients reconnect, run:

- release parity,
- health,
- native-surface and direct-library negative bypass checks.

Then run one ordinary task canary and one exact one-time-approved initiative
canary (the initiative canary), verifying readback, approval consumption,
transition/run lineage, notifications, and cleanup.

Gate: all negative bypass checks pass; both canaries complete with correct
readback, approval consumption, lineage, notifications, and cleanup.

## Phase 10 — Staged reactivation

Use staged reactivation in this order:

1. read-only health/projection,
2. gateway,
3. one Desktop client,
4. profile tools,
5. plugin dispatcher,
6. approved automation.

Retain the prior release and rollback set through soak.

Gate: each stage healthy before the next; soak period completes with the prior
release and rollback set retained.

## Rollback — stopped authority reversal

Rollback is a stopped authority reversal.

- If no accepted plugin mutation exists, restore the verified rollback set and
  native authority atomically with `restore_rollback_set`.
- After any accepted plugin mutation, blind restore is prohibited: require the
  verified reverse migration (`reverse_migration`) and explicit record
  reconciliation.
- On any failure, keep the system stopped and fail closed.

Gate: restored state verified; system remains stopped until explicitly cleared.
