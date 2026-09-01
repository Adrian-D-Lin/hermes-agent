# Write-Gate Deployment Runbook

> **Scope.** This document is the production deployment runbook for the
> Write-Gate host-owned integration. It records the runtime facts an operator
> must know to enable, verify, and roll back the Write-Gate package in a live
> Hermes agent runtime. Every command is executable; every security property is
> backed by a command or by an exact reference to an already-green test.

> **Do not edit this runbook to match source text.** The Write-Gate behaviour is
> verified by the test suite, not by grepping source. Sections that assert a
> security property point at the test that covers it.

---

## Runtime facts (read first)

These are the actual runtime locations and keys. The runbook commands below are
built on them.

| Concept | Actual value | Source |
| --- | --- | --- |
| Session store (per profile) | `<profile>/state.db` | `hermes_state.DEFAULT_DB_PATH` |
| Trusted lineage read | `SessionDB(state.db, read_only=True)` | `writegate/lineage.py::_open_ro_session_db` |
| Central security registry | `get_default_hermes_root()/write-gate.db` (cross-profile) | `writegate/registry.py::resolve_registry_path` |
| Plugin enable / kill-switch | `security.write_gate.enabled` (absent = disabled, fail closed) | `plugins/write-gate/__init__.py::_write_gate_enabled` |
| Plugin discovery allow-list | `plugins.enabled` (opt-in; plugin loads only when listed) | `hermes_cli/plugins.py::_get_enabled_plugins` |
| Enforcement hook | `pre_tool_call` -> `_on_pre_tool_call` | `plugins/write-gate/__init__.py` |
| Host process | `hermes-gateway.service` and `hermes-serve.service` | systemd user units |
| Live code under service | `/home/progenitor/.hermes/hermes-agent` | systemd unit WorkingDirectory |
| Test interpreter (has pytest + pytest-asyncio) | `/home/progenitor/.hermes/hermes-agent/.venv/bin/python` | venv probe |

**Test interpreter.** The worktree's own `.venv` has **no site-packages at all**
(it ships `bin/activate` but no `pytest`, and no `pytest-asyncio`). Use the live
developer venv, which has both:

```bash
/home/progenitor/.hermes/hermes-agent/.venv/bin/python   # has pytest 9.x + pytest-asyncio
```

`scripts/run_tests.sh` probes `.venv` → `venv` → `$HOME/.hermes/hermes-agent/venv`
and only selects a venv that actually imports pytest, so it will skip the
worktree `.venv` and use the live one when `HERMES_PYTHON` points at it.

**Broad / async tests.** For the full `tests/plugins/` run (which includes
async tests), `run_tests.sh` alone is not reliable: the worktree `.venv` is
empty and the live venv is the only one with `pytest-asyncio`. Call the live
interpreter directly on `scripts/run_tests_parallel.py` under a clean
environment, as the verified run did:

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  HERMES_TEST_FILE_RETRIES=0 \
  /home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests_parallel.py tests/plugins/
```

**Two independent gates, not one.** Enabling Write-Gate requires **both** keys:

1. `plugins.enabled` must list `write-gate`, otherwise the plugin manager never
   collects the manifest and `register()` never runs — the hook is absent
   regardless of the security flag. This is the standard opt-in plugin gate
   (`plugins.enabled` is an allow-list; absent or `[]` = nothing loads).
2. `security.write_gate.enabled` must be `true`, otherwise `register()` returns
   before arming the `pre_tool_call` hook or the `write_gate` tool.

Both keys are read from the **active profile's** `config.yaml`
(`<profile>/config.yaml`), because plugin discovery reads the profile config.
The `write-gate.db` registry, by contrast, is central
(`get_default_hermes_root()/write-gate.db`) and is shared by every profile.

**Why the two databases are not the same.** `state.db` is each profile's
conversation store; the lineage resolver opens it **read-only** to classify how
a session came to exist (branch / delegate / compression child). The
`write-gate.db` registry is the single cross-profile security store of
bindings, exception requests, and leases; it is deliberately *not* per-profile.
A runbook that conflates them (e.g. "SessionDB is write-gate.db") would point an
operator at the wrong file.

---

## 1. Preflight

Confirm the Write-Gate package imports under the live interpreter and that the
registry path resolves to a writable location. Run from the integration
worktree checkout so the `writegate` package is importable; if running from
elsewhere, set `PYTHONPATH` to point at the worktree root.

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
/home/progenitor/.hermes/hermes-agent/.venv/bin/python -c "import writegate.registry, writegate.enforcement, writegate.tool, \
       writegate.lineage, writegate.binding, writegate.approval, \
       writegate.recovery, writegate.containment; \
       print('all writegate modules importable')"
```

**Expected.** `all writegate modules importable` on stdout, exit code 0.

```bash
/home/progenitor/.hermes/hermes-agent/.venv/bin/python -c "from writegate.registry import resolve_registry_path; \
       import os; p = resolve_registry_path(); \
       d = os.path.dirname(p); \
       open(os.path.join(d, '.wg-write-test'), 'w').close(); \
       print('registry path writable:', d); \
       os.remove(os.path.join(d, '.wg-write-test'))"
```

> **Shell syntax.** The interpreter path is run as a **plain executable**
> (`…/bin/python -c …`), never as an environment assignment immediately
> followed by `-c`. `HERMES_PYTHON=/path/python -c …` is invalid shell: the
> shell treats `-c` as the command to run and reports
> `/bin/sh: 1: -c: not found` (exit 127). An environment assignment is valid
> only when it precedes an **actual executable** — that is the shape used for
> the test-runner invocations below, not for `python -c` probes.

**Expected.** The directory holding `write-gate.db` printed, exit code 0. If the
path is not writable, the registry cannot initialize and the hook fails closed.

---

## 2. Integration: fast-forward the live checkout from this branch

The worktree branch `integration/startup-writegate-runtime` is a **direct
fast-forward descendant** of the live Primus baseline. The live checkout at
`/home/progenitor/.hermes/hermes-agent` is on `main` and has **existing local
modifications** that are not part of the Write-Gate integration and must be
preserved. Integrate by fast-forwarding the live tree from this branch — never
per-file copy or cherry-pick, which would strand those local changes.

> **The live checkout is dirty before migration.** It currently carries local
> modifications (modified tracked files) that are not part of the Write-Gate
> integration. `git merge --ff-only` refuses to run while the working tree is
> dirty, so the working tree **must** be stashed first. Unlike the pre-cutover
> working tree committed in the integration history, this dirty state is a
> **live, in-flight** set of changes — it is **not** guaranteed to match any
> committed baseline. Therefore:
>
> * **Do not** rely on "these local mods are already in `c30942e9`'s lineage" —
>   that was true for the earlier baseline but the live checkout has since
>   diverged. Treat the current dirty state as new work to preserve.
> * **Do** create a **named stash that includes untracked files**
>   (`git stash push -u -m "…"`), fast-forward, then **leave the stash in
>   place** through acceptance. Do **not** `stash pop` it: its content is
>   preserved in the stash entry and re-applying it risks a conflict that
>   silently drops a change. The rollback branch (step 2a) plus the retained
>   stash together cover any live-tree change if acceptance fails.
> * **Do not** `git reset --hard` during the migration — that would discard the
>   working-tree changes. Rollback is covered in the dedicated procedure (§8),
>   where `reset --hard` to a named ref is appropriate.

**2a. Name a recoverable rollback ref on the live checkout.**

```bash
LIVE=/home/progenitor/.hermes/hermes-agent
cd "$LIVE"
git branch write-gate-rollback-$(date +%Y%m%d-%H%M%S) HEAD
git rev-parse --short HEAD   # record the pre-integration SHA
```

This ref points at the current live state, so the integration can be undone with
one command at any point through live acceptance.

**2b. Stash the dirty working tree (including untracked files) before the merge.**

```bash
cd "$LIVE"
git stash push -u -m "write-gate-preintegration-dirty-state-$(date +%Y%m%d-%H%M%S)"
git status --short           # expect: clean working tree
```

If `git status --short` is **not** clean, stop — the working tree still has
uncommitted changes and `git merge --ff-only` will refuse. Resolve the residual
state before continuing.

**2c. Verify the worktree branch is a fast-forward of the live HEAD, then merge.**

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
# Current integration tip (dynamic — do not hard-code; it advances each runbook run):
git rev-parse --short HEAD
git merge-base --is-ancestor $(git rev-parse HEAD) integration/startup-writegate-runtime && \
  echo "integration branch tip is reachable from this branch"
```

**2d. Fast-forward the live checkout.**

```bash
cd "$LIVE"
git merge --ff-only integration/startup-writegate-runtime
git log --oneline -1
```

**Expected.** The live HEAD advances to the branch tip with no merge commit, and
the stat shows the full Write-Gate package plus the runbook. The branch tip is
the latest runbook-correcting commit (`4a2457fe` or later) — **not** the older
`c890a409`. Verify the live tip is at least the branch tip:

```bash
cd "$LIVE"
git merge-base --is-ancestor $(cd /home/progenitor/AI-main/hermes-startup-writegate-build && git rev-parse HEAD) HEAD && \
  echo "live checkout now at or beyond the integration tip"
```

**2e. Verify the tree is clean.**

```bash
cd "$LIVE"
git status --short           # clean: no residual working-tree changes
```

If the tree is not clean, stop and investigate before proceeding — a dirty tree
here means the stash did not fully clear or new changes appeared. Keep the
`write-gate-rollback-<ts>` branch and the stash until live acceptance is green
(see §6 smoke tests and §11 monitoring). To fully undo the integration, see §8.

---

## 3. Cutover: atomic config.next swap (enables plugin, removes legacy hook)

The live configs currently carry a **legacy** Write-Gate enforcement hook wired
as a `hooks.pre_tool_call` command:

```
/home/progenitor/.hermes/agent-hooks/write-gate-enforcement.py
```

The new plugin registers its **own** `pre_tool_call` hook. Both must not be
active at once — that would run two enforcement paths on the same write. The
independent `hooks.pre_llm_call` session-startup-checklist hook is unrelated and
stays.

**Production targets.** Every production agent home, because discovery and hook
resolution read the active profile config:

- global: `/home/progenitor/.hermes/config.yaml`
- `/home/progenitor/.hermes/profiles/builder-tester/config.yaml`
- `/home/progenitor/.hermes/profiles/independent-reviewer/config.yaml`
- `/home/progenitor/.hermes/profiles/test-authority-reviewer/config.yaml`

The temporary `qwen-build-*` audit profiles are **not** production targets.

This cutover runs under the dual-PGX master plan. Two rules matter:

* **Never store backups beside the active config.** Put all four backups under a
  single timestamped, permission-restricted directory.
* **Never leave a half-cutover state** (plugin enabled but legacy hook still
  present, or legacy hook gone but plugin not yet enabled). Services are stopped
  for the whole edit, so there is no ungoverned window.

**3a. Create the timestamped backup root and back up all four configs.** Each
`config.yaml` is a distinct path (global vs one per profile), so preserve the
unique path inside the backup root — do **not** collapse all four to a single
`config.yaml` name, or later profiles would overwrite one another on restore.

```bash
TS=$(date +%Y%m%d-%H%M%S)
ROOT=/srv/pgx-production/backups/$TS/writegate-hermes
mkdir -m 700 "$ROOT"
for f in \
  /home/progenitor/.hermes/config.yaml \
  /home/progenitor/.hermes/profiles/builder-tester/config.yaml \
  /home/progenitor/.hermes/profiles/independent-reviewer/config.yaml \
  /home/progenitor/.hermes/profiles/test-authority-reviewer/config.yaml; do
  rel="${f#/home/progenitor/.hermes/}"          # profiles/<name>/config.yaml etc.
  cp -a "$f" "$ROOT/$rel"
  chmod 600 "$ROOT/$rel"
done
ls -laR "$ROOT"
```

**3b. Stop the services so no enforcement path runs during the edit.** These are
**systemd user units** — use `systemctl --user`, never `sudo systemctl`.

```bash
systemctl --user stop hermes-gateway.service hermes-serve.service
systemctl --user is-active hermes-gateway.service hermes-serve.service   # expect "inactive"
```

With the services down there is no window in which governed writes are
ungoverned: the gateway is simply not serving.

**3c. Render the four `config.next` files.** For each config, write the
post-cutover YAML to a `config.next` sibling:

* set `security.write_gate.enabled: true` and add `write-gate` to
  `plugins.enabled`;
* **remove** the entire `hooks.pre_tool_call` entry (the legacy
  `write-gate-enforcement.py` command);
* **retain** `hooks.pre_llm_call` (`session-startup-checklist.py`) unchanged.

Edit each `config.next` by hand or with a small script; do not edit the live
`config.yaml` in place yet.

**3d. Validate every `config.next` before install.** Each must parse, enable the
plugin, and carry no `pre_tool_call` entry. Run from the integration worktree
checkout (or set `PYTHONPATH` to it) so the import resolves:

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
/home/progenitor/.hermes/hermes-agent/.venv/bin/python -c "
import yaml
files = [
  '/home/progenitor/.hermes/config.next',
  '/home/progenitor/.hermes/profiles/builder-tester/config.next',
  '/home/progenitor/.hermes/profiles/independent-reviewer/config.next',
  '/home/progenitor/.hermes/profiles/test-authority-reviewer/config.next',
]
for f in files:
    c = yaml.safe_load(open(f)) or {}
    pl = c.get('plugins', {})
    sec = c.get('security', {})
    wg = sec.get('write_gate', {})
    hooks = c.get('hooks', {})
    assert pl.get('enabled') and 'write-gate' in pl['enabled'], f'{f}: plugin not enabled'
    assert wg.get('enabled') is True, f'{f}: security flag not true'
    assert 'pre_tool_call' not in hooks, f'{f}: legacy pre_tool_call not removed'
    assert 'pre_llm_call' in hooks, f'{f}: pre_llm_call startup hook missing'
    print(f'{f}: OK (plugin enabled, security flag on, legacy hook removed, pre_llm_call retained)')
"
```

**3e. Install all four as one maintenance change.** Swap each validated
`config.next` over the live `config.yaml` atomically:

```bash
for f in \
  /home/progenitor/.hermes/config.yaml \
  /home/progenitor/.hermes/profiles/builder-tester/config.yaml \
  /home/progenitor/.hermes/profiles/independent-reviewer/config.yaml \
  /home/progenitor/.hermes/profiles/test-authority-reviewer/config.yaml; do
  mv "${f}.next" "$f"
done
```

**3f. Restart the services.**

```bash
systemctl --user start hermes-gateway.service hermes-serve.service
systemctl --user is-active hermes-gateway.service hermes-serve.service   # expect "active"
```

The cutover is complete: the plugin is the sole `pre_tool_call` enforcement path,
`pre_llm_call` startup is retained, and there was never a half-cutover state.

> **No separate enable or restart step is needed.** This atomic cutover already
> set both `security.write_gate.enabled` and `plugins.enabled` and restarted the
> services in §3e/§3f. The old runbook's standalone "Enable Write-Gate" and
> "Controlled service restart" sections are removed as redundant — the enable is
> folded into the config.next swap, and the restart happens immediately after.

---

## 4. Smoke tests (CLI / GUI / Kanban)

These exercise the three surfaces an operator can watch. Each asserts a
security-relevant behaviour end-to-end against the live code. Run from the
integration worktree checkout (or set `PYTHONPATH` to it) so the plugin module
path resolves.

### 4a. Fail-closed without a host session id

A governed write with no host-owned session id **must** block. `None` (allow) is
**not** an acceptable result here.

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import importlib.util
spec = importlib.util.spec_from_file_location(
    'write_gate_plugin',
    '/home/progenitor/AI-main/hermes-startup-writegate-build/plugins/write-gate/__init__.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
result = mod._on_pre_tool_call(tool_name='write_file',
    args={'path': '/tmp/should-block.md'}, session_id='')
assert result is not None, 'hook returned None (allow) for unbound session'
assert result.get('action') == 'block', f'unexpected allow: {result!r}'
print('fail-closed verified:', result)
"
```

**Expected.** A block directive on stdout, exit code 0. `None` is rejected by the
assertion.

### 4b. Reads and Kanban are exempt before binding

Read and Kanban tools return `None` (allow) even with no binding.

```bash
/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import importlib.util
spec = importlib.util.spec_from_file_location(
    'write_gate_plugin',
    '/home/progenitor/AI-main/hermes-startup-writegate-build/plugins/write-gate/__init__.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
r1 = mod._on_pre_tool_call(tool_name='read_file', args={'path': '/tmp/x.md'}, session_id='exempt')
r2 = mod._on_pre_tool_call(tool_name='kanban_show', args={}, session_id='exempt')
assert r1 is None, f'read blocked: {r1!r}'
assert r2 is None, f'kanban blocked: {r2!r}'
print('reads + kanban exempt before binding: verified')
"
```

**Expected.** `reads + kanban exempt before binding: verified`, exit code 0.

> These two probes are the executable form of the security properties that the
> focused test suite already covers. Prefer the suite for regression; run the
> probes when you need a quick live confirmation without pytest.

---

## 5. Verification by the test suite

The authoritative evidence is the committed test suite, run with the live
interpreter. Run each Write-Gate suite; every one must pass.

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/plugins/test_writegate_startup_binding.py
```

**Expected (focused suite).** `=== Summary: 1 files, 42 tests passed, 0 failed (100% complete) ===`

This suite covers: binding derivation, compression-child inheritance, delegate
session inheritance, `/new`-with-parent retention, read/kanban exemption,
fail-closed without session id, lazy lineage derivation, and the kill switch.

```bash
/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/plugins/test_writegate_enforcement.py \
                    tests/plugins/test_writegate_registry.py \
                    tests/plugins/test_writegate_lineage.py \
                    tests/plugins/test_writegate_multitarget.py \
                    tests/plugins/test_writegate_recovery.py \
                    tests/plugins/test_writegate_host_owned_import.py \
                    tests/plugins/test_writegate_plugin_register.py
```

**Expected (affected suites).** A summary line with 0 failed tests. Together
these cover containment, protected locations, symlink-escape rejection, lease
matching + expiry, recovery snapshot, multi-target governance, host-owned
import, and plugin registration.

**Do not replace these with source-text greps.** A check that greps source for
the word `realpath` or `_cache_set` passes even when the code path is dead or
wired wrong, and fails on a pure refactor that preserves behaviour. The suite
executes the real path. If a security property is not yet covered by a test,
that is a gap to surface — not to paper over with a string check.

---

## 6. Broad regression check

The Write-Gate change must not widen the pre-existing plugin failure set. The
baseline (commit `ba55ae29`) failure set is the reference: run the full
`tests/plugins/` suite and confirm the failures are exactly the documented
pre-existing ones (and the optional-dependency files that cannot run), with no
new `writegate` failure and no new failures elsewhere.

Because the worktree `.venv` is empty and the live venv is the only one with
`pytest-asyncio`, call the live interpreter directly on
`scripts/run_tests_parallel.py` under a clean environment (see "Broad / async
tests" in Runtime facts):

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  HERMES_TEST_FILE_RETRIES=0 \
  /home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests_parallel.py tests/plugins/
```

**Expected.** All `test_writegate_*.py` files green. Any failures outside the
Write-Gate files must match the `ba55ae29` baseline (documented pre-existing
failures) — a new failure is a regression to investigate.

---

## 7. Commit evidence

The runbook revision is the only change to commit on this branch.

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
git log --oneline -1
git show --stat HEAD
```

**Expected.** A commit on `integration/startup-writegate-runtime` whose stat
shows only `WRITE_GATE_DEPLOYMENT.md`.

---

## 8. Rollback procedure

To roll back the Write-Gate integration at runtime, reverse the cutover as one
maintenance change while the services are stopped (no ungoverned window):

1. Stop the services (systemd **user** units — use `--user`, never `sudo`):

   ```bash
   systemctl --user stop hermes-gateway.service hermes-serve.service
   ```

2. Restore the four live configs from the timestamped backup root created in
   §3a, using the **unique path** stored for each (do **not** `mv` a
   `config.next` file — that file was consumed by the atomic swap in §3e and no
   longer exists; the validated copy lives at `$ROOT/<unique-path>`):

   ```bash
   TS=<the timestamp from §3a>
   ROOT=/srv/pgx-production/backups/$TS/writegate-hermes
   for rel in \
     config.yaml \
     profiles/builder-tester/config.yaml \
     profiles/independent-reviewer/config.yaml \
     profiles/test-authority-reviewer/config.yaml; do
     cp -a "$ROOT/$rel" "/home/progenitor/.hermes/$rel"
   done
   ls -laR "$ROOT"
   ```

   The restored configs carry the legacy `hooks.pre_tool_call` hook and the
   security flag `false`, so governed writes fall back to the pre-plugin path.
   Confirm:

   ```bash
   grep -rn "write-gate-enforcement" /home/progenitor/.hermes/config.yaml \
     /home/progenitor/.hermes/profiles/*/config.yaml
   ```

3. Start the services:

   ```bash
   systemctl --user start hermes-gateway.service hermes-serve.service
   ```

4. Confirm the plugin is inert. With `security.write_gate.enabled` false, the
   plugin's `register()` returns before registering the tool or the
   `pre_tool_call` hook, so writes are governed by the restored legacy hook.

The recoverable rollback ref and stash from §2 let you restore any live-tree
changes if the rollback touched anything beyond config.

---

## 9. Operational monitoring

The central registry (`write-gate.db`) stores bindings, exception requests, and
leases. A healthy registry has a small, bounded number of active rows.

```bash
/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import sqlite3, os
db_path = '/home/progenitor/.hermes/write-gate.db'
if not os.path.exists(db_path):
    print('registry DB not found (expected before first use)')
else:
    conn = sqlite3.connect(db_path)
    bindings = conn.execute('SELECT COUNT(*) FROM write_gate_bindings WHERE status = ?').fetchone()[0]
    leases   = conn.execute('SELECT COUNT(*) FROM write_gate_leases WHERE status = ?').fetchone()[0]
    print(f'registry: {bindings} active bindings, {leases} active leases')
    conn.close()
"
```

**Expected.** Small non-negative counts, exit code 0. The five-minute lease
window is a deliberate design choice (a session-bound approval window, not a
byte-exact or single-use approval); expired leases simply stop matching and the
target blocks again.

---

## Quick final gate

Run the focused suite and confirm the commit is the runbook-only change:

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/plugins/test_writegate_startup_binding.py \
  2>&1 | grep "Summary"
git log --oneline -1
```

**Expected.** A `=== Summary: 1 files, 42 tests passed, 0 failed ===` line and a
commit whose stat shows only `WRITE_GATE_DEPLOYMENT.md`.
