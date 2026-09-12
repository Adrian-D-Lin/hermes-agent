# Initiative Tracker v0.29 stopped-upgrade runbook

This is the single operational runbook for upgrading the deployed Initiative
Tracker (`adrian-kanban`) to the v0.29 Session Startup and coordination-workspace
release. It does not authorize a production mutation. Stop at every failed gate,
keep all writers stopped, and use only the verified rollback or recovery route.

## 1. Dark stage one immutable release

1. Commit the complete server/plugin, bundled-skill, Canon, test, and Desktop
   compatibility set.
2. Build an immutable release package beside—not over—the live Hermes tree.
3. Record the source commit, package digest, Desktop payload digest, release
   identity, database identity, and configuration digest with
   `build_release_manifest`.
4. Run `verify_dark_install` and `plan_dark_install`. Every finding must pass;
   the plan must remain inactive and must not change the live pointer.

Gate: one verified package and one matching Desktop extension exist; deployed
v0.28 remains authoritative.

## 2. Validate production configuration

Every runtime—the gateway, serve process, dispatcher, private adapter, four
profiles, and Desktop presentation—must resolve the same absolute
`kanban.database_path`, plugin release, protocol, schema, registry, skill-bundle,
and Desktop protocol identity. Mixed-version operation fails closed.
In dotted notation, `kanban.mutation_authority` must resolve to
`adrian-kanban`; any other or mixed identity must fail closed.

The gateway configuration must also contain:

```yaml
kanban:
  mutation_authority: adrian-kanban
  controlled_worktree_root: /home/progenitor/AI-worktrees
  repository_registry:
    <repository-id>:
      repository_root: <absolute-primary-clone>
```

Each active Hermes Project with a Tracker `board_slug` must have one absolute
`primary_path` that resolves to exactly one registered repository. Do not infer
project/repository membership from CWD, chat, historical card text, or filesystem
discovery.

Gate: configuration and project-to-primary-repository mapping validate without
ambiguity.

## 3. Open the stopped maintenance window

Close all Desktop clients and stop:

- `hermes-gateway.service`;
- `hermes-serve.service`;
- plugin/native dispatchers;
- workers and profile processes; and
- all Kanban-writing automation.

```bash
systemctl --user stop hermes-gateway.service hermes-serve.service
systemctl --user is-active hermes-gateway.service hermes-serve.service
```

Confirm no active/claimed task run, model turn, writable Tracker connection, or
background migration remains.

Gate: every writer is stopped.

## 4. Capture and verify rollback

Use `capture_rollback_set` to retain configuration, the live release pointer and
prior immutable package, matching Desktop build, Tracker database with consistent
WAL/SHM state, WriteGate registry, profile configuration, legacy startup-hook
configuration, schema/migration metadata, counts, hashes, and health output.

Run `verify_rollback_set` and verify the copied database:

```bash
sqlite3 "$ROLLBACK_DIR/database.sqlite" "PRAGMA integrity_check;"
```

Gate: every rollback finding passes and SQLite reports `ok`.

## 5. Verify additive schema on a disposable copy

Copy the stopped Tracker database to a disposable path. Load the v0.29 plugin so
its idempotent schema initializer adds Session Startup, coordination workspace,
multi-root binding, operation journal, and closure journal records. Run the core
regression and health checks against the copy. Existing initiative/task content
must be unchanged.

Legacy card transposition is a separate completed migration concern. Do not rerun
or reinterpret it during this upgrade.

Gate: schema initialization is repeatable; integrity, counts, and existing
projections agree.

## 6. Plan coordination backfill without writes

From the immutable release root, run the maintenance command with no `--apply`:

```bash
/home/progenitor/.hermes/hermes-agent/venv/bin/python \
  -m plugins.adrian-kanban.maintenance_backfill
```

Review the complete JSON plan. It must:

- include every open initiative exactly once;
- skip formally closed initiatives;
- map each initiative board to exactly one active Hermes Project;
- map each project `primary_path` to exactly one registered repository;
- create only the primary repository member; and
- classify an already matching workspace as existing.

Any missing, duplicate, or conflicting mapping blocks the entire apply attempt.
Additional repositories are not backfilled automatically; propose them later,
initiative by initiative, through the approved membership-expansion operation.

Gate: Adrian accepts the reviewed primary-only plan.

## 7. Activate the immutable server release and schema

Atomically switch the server release pointer to the immutable v0.29 package.
Initialize the live additive schema once and rerun it to prove idempotency. Keep
gateway, serve, dispatchers, and clients stopped.

Gate: release health reports the exact package/database identity and no native
public mutation fallback.

## 8. Apply and verify primary-only backfill

Run:

```bash
/home/progenitor/.hermes/hermes-agent/venv/bin/python \
  -m plugins.adrian-kanban.maintenance_backfill --apply
```

Immediately rerun plan mode. It must report no planned creations and every open
initiative as an existing matching workspace. This operation plans Tracker
identities only; it does not create physical Git worktrees. Those materialize
lazily on first selected use.

Gate: backfill apply is idempotent and readback agrees with the accepted plan.

## 9. Retire the legacy startup hook

Disable the legacy external `pre_llm_call` Session Startup hook and remove any
profile instruction that injects `Canon/session-startup-checklist.md`. Do not
disable WriteGate. The plugin-owned `pre_user_turn` callback must be the sole
Session Startup authority.

Profile contract:

- top-level `default`: interactive Session Startup enabled;
- MoA selected inside `default`: enabled;
- nested child or Kanban worker: excluded and pre-bound by trusted lineage;
- `builder-tester`, `independent-reviewer`, `test-authority-reviewer`: excluded;
- all profiles: Write-Gate enforcement remains enabled.

Gate: registration tests prove exactly one startup owner and the required profile
scope.

## 10. Pre-reconnect verification

Run release parity, runtime health, schema integrity, negative bypass checks for
the native surface, the 537-test integrated v0.29 core suite (or its later
superseding baseline), and focused real-Git closure/archive tests.

Run stopped or isolated canaries for:

- exhaustive project and active-initiative lists;
- read-only closed browsing;
- title/objective-only creation with full-payload WriteGate preview;
- coordination and segment workspace derivation;
- multi-member WriteGate containment;
- dirty/divergent/unexplained advancement rejection;
- exact-once held-prompt release;
- reconnect and compaction reuse; and
- close → verified archive → coordination retirement recovery.

Gate: all checks pass with no model call during startup control exchanges.

## 11. Staged reactivation and production soak

Reactivate in order:

1. read-only health/projection;
2. gateway;
3. one Desktop client;
4. one new default-profile Session Startup canary;
5. existing-session reconciliation canary;
6. profile tools;
7. plugin dispatcher; and
8. approved automation.

Confirm the laptop/PC Desktop extension reports compatible protocol identity.
The Desktop extension remains a presentation surface; the gateway owns state and
policy. Retain the prior release and rollback set through soak.

Gate: every stage is healthy before the next and soak completes without native
fallback, duplicated startup, binding drift, or journal backlog.

## 12. Rollback and forward recovery

Before any v0.29 startup row, coordination record, or accepted mutation exists,
the verified stopped rollback set may restore the prior pointer, configuration,
database, and Desktop build.

After v0.29 state or an accepted plugin mutation exists, a blind restore of
files or the database is prohibited. Keep the system stopped and classify the state:

- additive schema/backfill records may remain only if the prior release is
  proven to ignore them safely;
- an issued held prompt or changed binding requires exact session reconciliation;
- a prepared/failed coordination or closure journal must resume or halt through
  its recorded recovery stage;
- an accepted Tracker mutation requires verified reverse migration or explicit
  forward repair and record reconciliation.

Never reset, delete a worktree, discard a commit, revive a closed initiative, or
fabricate a journal checkpoint to force rollback. Restart services only after
the restored or recovered state passes the same health and integrity gates.
