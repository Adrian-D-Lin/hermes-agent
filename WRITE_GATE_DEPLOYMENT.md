# WriteGate Deployment & Trust Handshake

This document describes the WriteGate plugin, its two enforcement surfaces
(``pre_tool_call`` hook + ``write_gate`` tool), and the dispatcher→worker
preassignment handshake that ties a Kanban worker's transcript session id to a
central worktree binding.

## Components

| Path | Role |
|------|------|
| `plugins/write-gate/__init__.py` | Plugin entrypoint. Registers the tool + hook only when `security.write_gate.enabled`. Thin registration surface. |
| `writegate/enforcement.py` | `decide()` — fail-closed gate on governed mutations. Reads the **host-owned** `session_id` from hook kwargs. |
| `writegate/tool.py` | `write_gate_tool()` — the service-gated control tool. Uses only the host-owned `session_id` kwarg. |
| `writegate/registry.py` | Central binding registry (`~/.hermes/write-gate.db`). Shared primitive, importable by the dispatcher. |
| `writegate/binding.py` | `TrustedBindingProducer` — derives/confirm binding candidates from trusted state, never model paths. |
| `writegate/approval.py` | `request_write_gate_approval()` — the host-owned, `once`-only approval primitive. |
| `writegate/recovery.py` | `RecoveryWriter` + `compute_recovery_location()` — out-of-worktree exception recovery. |
| `writegate/containment.py` | `canonicalize_target()` — path containment checks. |
| `hermes_cli/kanban_db.py` | `prepare_worker_launch()` (dispatcher side) + `_trusted_worker_session_id()` (CLI side). |
| `cli.py` | `_resolve_preassigned_worker_session_id()` — honors the preassigned id when trusted markers agree. |

## Trust model

The `session_id` is **host-owned**. `model_tools` forwards it into
`registry.dispatch` kwargs; the tool and hook read it from there. The model may
**not** supply `session_id`, `confirmed_worktree`, `project`, `board`,
`profile`, `lease_id`, `request_id`, or `recovery_location` as authority — those
values are ignored when they appear in the model's argument payload.

### Security review gate (build brief)

* `tools/registry.py` `dispatch()` forwards the **host-owned** `session_id`
  through `**kwargs` (verified: `tools/registry.py:1128`).
* `model_tools.py` passes `session_id` into dispatch kwargs
  (verified: `model_tools.py:1536`).
* `__init__.py` handler reads `session_id=str(kw.get("session_id"))` from
  `**kw`, not from `**args`.
* `_on_pre_tool_call` reads `session_id` from `kwargs` only — **no `args`
  fallback**. A model-supplied `session_id` is never trusted.
* `tool.py` rejects model-supplied authority fields; resolves binding/worktree
  from trusted state.

## Dispatcher→worker preassignment handshake

The dispatcher and worker must agree on **one** worker session id so the
worker's `state.db` transcript row and the central WriteGate binding share the
same id (lineage). This prevents a worker from racing ahead of its binding.

### Dispatcher side (`prepare_worker_launch`)

1. Generate one normal Hermes worker session id: `<timestamp>_<uuid4-hex-6>`,
   derived from `task_id` + `run_id` (same shape the worker itself generates).
2. Persist it on the exact `task_runs` row (`worker_session_id` column) via
   `_persist_worker_session_id` — match on the exact run `id` + `task_id`.
3. Create the central active binding and read it back, verifying
   profile / workspace / board lineage. Fail-closed: if the binding cannot be
   created or verified, raise `WriteGatePreSpawnError` so the prepared binding
   is abandoned and the spawn aborted.
4. Return the captured id so the caller can (a) pass it to `_default_spawn`
   (sets `HERMES_KANBAN_WORKER_SESSION_ID` on the launch env) and (b) abandon
   it on failure.

### CLI side (`_trusted_worker_session_id`)

The worker honors `HERMES_KANBAN_WORKER_SESSION_ID` as *its own* session id
only when every trusted marker agrees. Verification (all must hold, else
return `None`):

* the supplied `task_id` has a run row whose primary key `id` equals `run_id`
  and whose `task_id` equals `task_id`;
* that run row carries a non-null `worker_session_id` equal to the preassigned
  value;
* the task's `assignee` matches `profile` when a profile is supplied;
* the task's canonical workspace matches `workspace` when supplied;
* the board database/slug is resolvable when supplied;
* the central WriteGate registry has an *active* binding whose `session_id`
  equals the preassigned id and whose stored worktree, board, task/initiative,
  and profile lineage match the run record.

A missing/failed check anywhere returns `None` — the worker then generates its
own id and stays unbound (read-only until a trusted binding exists).

### CLI wiring (`cli.py`)

`HermesCLI._resolve_preassigned_worker_session_id()` reads
`HERMES_KANBAN_TASK` / `HERMES_KANBAN_RUN_ID` / `HERMES_KANBAN_BOARD` /
`HERMES_KANBAN_WORKSPACE` / `HERMES_KANBAN_PROFILE` /
`HERMES_KANBAN_WORKER_SESSION_ID` from the launch env and returns the trusted
id only when `_trusted_worker_session_id()` confirms it. Otherwise the worker
keeps its own generated id.

## Import path (shared primitives)

The shared WriteGate primitives are a **repo-root host package** (`writegate/`)
importable before plugin discovery — not a plugin-private tree. The dispatcher
shares `writegate.registry` with the plugin. `_ensure_writegate_importable()`
in `kanban_db.py` adds the repository root to `sys.path` only if `writegate`
is not yet resolvable, so the pre-spawn binding handshake does not depend on
plugin-registration ordering. The thin plugin `__init__.py` performs the same
guard so the registration surface works regardless of loader or ordering.

## Testing

Focused suites:

* `tests/hermes_cli/test_kanban_writegate_preassignment.py` — dispatcher
  preassignment + trusted-worker validation, mismatched-marker and
  spawn-failure cases, tool authority spoofing.
* `tests/plugins/test_writegate_enforcement.py` — `decide()` gate + tool entrypoint.
* `tests/plugins/test_writegate_recovery.py` — recovery-writer lineage.
* `tests/plugins/test_writegate_plugin_register.py` — gated registration.
* `tests/plugins/test_writegate_host_owned_import.py` — repo-root host-package
  importability (Gate 1): proves `writegate` resolves without a plugin-private
  `sys.path` insertion.
* `tests/plugins/test_writegate_lineage.py` — binding lineage (resume, branch,
  delegate, supersession, confirmed-binding history).
* `tests/plugins/test_writegate_multitarget.py` — multi-target preflight,
  recovery evidence, move governs both endpoints, exact lease worktree equality.
* `tests/tools/test_writegate_approval_correlation.py` — approval identity and
  correlation: wrong/absent id fails closed, generic approvals cannot authorize,
  gateway-without-notify fails closed, reconnect replay, host reference.
* `tests/tools/test_writegate_approval_primitive.py` — approval fast-path + selected-transport.

Run with the CI-parity runner:

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  bash scripts/run_tests.sh tests/hermes_cli/test_kanban_writegate_preassignment.py \
  tests/plugins/test_writegate_*.py tests/tools/test_writegate_*.py -q
```

## Failure modes

* **Injected `HERMES_KANBAN_WORKER_SESSION_ID`** — fails closed: the CLI-side
  lookup requires the exact run row + central binding lineage. An arbitrary
  env value returns `None`.
* **Worker restart / new run** — `_generate_worker_session_id` derives a new id
  per run, so a retry yields a new binding.
* **Binding verification failure** — raises `WriteGatePreSpawnError`; the
  prepared binding is abandoned and the spawn is aborted (hard failure).
* **`security.write_gate.enabled` false** — no binding handshake; dispatcher
  behaves as before.
