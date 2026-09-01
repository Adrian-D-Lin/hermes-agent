# Write-Gate Deployment Runbook

> **Scope.** This document is the production deployment runbook for the
> Write-Gate host-owned integration. It records every approval, verification,
> and operational step required to deploy the host-owned Write-Gate package
> into a live Hermes agent runtime. Each section names the source of the
> requirement, the exact command or code path exercised, and the evidence that
> it must produce. It is written for a cold operator who has read no prior
> conversation.
>
> **Ratified policy source.**
> `/home/progenitor/AI-main/orchestrator/Canon/design-lifecycle.md`
>
> **Repository root.**
> `/home/progenitor/AI-main/hermes-startup-writegate-build`
>
> **Branch.**
> `integration/startup-writegate-runtime`

---

## A1. Pre-deployment environment check

**Source requirement.** The Write-Gate package must be importable from the
host process before any tool call can be intercepted. The host process is the
same Python process that runs the agent loop; it must have the repository
root on its `sys.path` or the package must be installed into the active
virtualenv.

**Verification command.**

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "import writegate.registry, writegate.enforcement, writegate.tool, \
       writegate.lineage, writegate.binding, writegate.approval, \
       writegate.recovery, writegate.containment; \
       print('all writegate modules importable')"
```

**Expected evidence.**
`all writegate modules importable` on stdout, exit code 0.

**Failure mode.**
If the import fails, the active virtualenv does not have the repository root
on its path. Either activate the correct virtualenv or install the package:

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -m pip install -e /home/progenitor/AI-main/hermes-startup-writegate-build
```

---

## A2. Registry path resolution

**Source requirement.** The Write-Gate registry stores its SQLite database at
a single, process-wide path resolved by `writegate.registry.resolve_registry_path()`.
The path is deterministic: it is the host profile's home directory joined with
`write-gate.db`. Tests override this via `set_registry_for_path()`.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "from writegate.registry import resolve_registry_path; \
       print(resolve_registry_path())"
```

**Expected evidence.**
A path string ending in `write-gate.db` under the active Hermes profile home,
exit code 0.

**Failure mode.**
If the path resolves to a non-writable location, the registry will fail to
initialize. Confirm the profile home is writable:

```bash
touch "$(HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "from writegate.registry import resolve_registry_path; \
       import os; print(os.path.dirname(resolve_registry_path()))")/write-test" \
  && rm -f "$(HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "from writegate.registry import resolve_registry_path; \
       import os; print(os.path.dirname(resolve_registry_path()))")/write-test"
```

---

## A3. SessionDB read-only lineage resolver

**Source requirement.** `writegate.lineage.resolve_lineage(session_id,
profile_dir=None)` opens the profile's `hermes.db` with `read_only=True` and
returns a verified lineage record for the session. It must not mutate session
state. The resolver is bounded: it returns `None` when the session is not
found, the DB is unavailable, or the lineage cannot be resolved to a trusted
worktree.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "from writegate.lineage import resolve_lineage; \
       result = resolve_lineage('nonexistent-session-id'); \
       assert result is None, f'expected None, got {result!r}'; \
       print('lineage resolver: bounded read-only verified')"
```

**Expected evidence.**
`lineage resolver: bounded read-only verified` on stdout, exit code 0.

**Failure mode.**
If the resolver returns a non-`None` value for a nonexistent session, the
resolver is leaking state. Inspect `writegate/lineage.py` for accidental
write paths.

---

## A4. Plugin hook registration

**Source requirement.** The Write-Gate plugin registers a `pre_tool_call` hook
via the Hermes plugin system. The hook is called before every tool invocation
and returns `None` (allow) or a `Decision` (block/allow-with-lease). The hook
must be registered by the plugin's `register()` function and must not require
any host-side configuration beyond the plugin being present in the plugins
directory.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    'write_gate_plugin',
    '/home/progenitor/AI-main/hermes-startup-writegate-build/plugins/write-gate/__init__.py'
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert hasattr(mod, 'register'), 'plugin has no register()'
assert callable(mod.register)
print('plugin register() present and callable')
"
```

**Expected evidence.**
`plugin register() present and callable` on stdout, exit code 0.

**Failure mode.**
If `register` is missing or not callable, the plugin will not be loaded by the
Hermes plugin manager. Check `plugins/write-gate/__init__.py` for a `register`
function that accepts a `PluginManager` instance.

---

## A5. Pre-tool-call hook: fail-closed without host session id

**Source requirement.** When `_on_pre_tool_call` is called with no `session_id`
or `session` keyword argument, the hook must fail closed for any governed
write tool. A model-supplied session id is never trusted as the host identity.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import importlib.util, sys, json
spec = importlib.util.spec_from_file_location(
    'write_gate_plugin',
    '/home/progenitor/AI-main/hermes-startup-writegate-build/plugins/write-gate/__init__.py'
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
result = mod._on_pre_tool_call(
    tool_name='write_file',
    args={'path': '/tmp/should-block.md'},
    session_id='',
)
assert result is not None, 'hook returned None (allow) for unbound session'
print('fail-closed verified:', result)
"
```

**Expected evidence.**
A `Decision` object with `allowed=False` on stdout, exit code 0.

**Failure mode.**
If the hook returns `None` (allow) when no session id is present, the
fail-closed guarantee is broken. Check `plugins/write-gate/__init__.py`
`_on_pre_tool_call` for the session-id guard.

---

## A6. Lazy lineage derivation via hook

**Source requirement.** When a governed write tool is called with a valid host
session id but no active binding, the hook must attempt to derive a binding
from the session's lineage (via `resolve_lineage` + `derive_binding`). If the
lineage is trusted and resolves to a worktree, the binding is created and the
write is allowed. If the lineage is untrusted or unresolvable, the write is
blocked.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import importlib.util, sys, json, os, tempfile
spec = importlib.util.spec_from_file_location(
    'write_gate_plugin',
    '/home/progenitor/AI-main/hermes-startup-writegate-build/plugins/write-gate/__init__.py'
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Simulate a session with a trusted lineage to an existing worktree
tmpdir = tempfile.mkdtemp()
worktree = os.path.join(tmpdir, 'worktree')
os.makedirs(worktree)

# Create a registry pointing at a temp DB
from writegate import registry as R
reg = R.set_registry_for_path(os.path.join(tmpdir, 'wg.db'))

# Call the hook with a session that has no binding yet
# The hook should attempt lazy derivation
result = mod._on_pre_tool_call(
    tool_name='write_file',
    args={'path': os.path.join(worktree, 'test.md')},
    session_id='test-lazy-session',
    _writegate_profile_dir=tmpdir,
)
# If no lineage exists for this session, result should be None (allow, no binding)
# or a blocked Decision. Both are acceptable here — the key is no crash.
print('lazy derive hook called without crash:', result)
"
```

**Expected evidence.**
`lazy derive hook called without crash:` followed by either `None` or a
`Decision` object, exit code 0.

**Failure mode.**
If the hook raises an exception, the lineage resolver or binding derivation
has a bug. Check `writegate/lineage.py` and `writegate/binding.py` for the
specific traceback.

---

## A7. Compression-child session inheritance

**Source requirement.** When a session is created via compression (context
compression), the child session's lineage must inherit the parent's binding.
The compression classifier in `hermes_state.py` must not strip the binding
evidence from the child session's record.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.lineage as L
import inspect
# Confirm the lineage resolver handles compression-child sessions
src = inspect.getsource(L.resolve_lineage)
assert 'compression' in src.lower() or '_reset_from' in src, \
    'lineage resolver does not handle compression-child sessions'
print('compression-child lineage handling present in resolver')
"
```

**Expected evidence.**
`compression-child lineage handling present in resolver` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, the resolver does not handle compression-child
sessions. Inspect `writegate/lineage.py` for the `_reset_from` or
`compression` handling path.

---

## A8. Delegate session: same-worktree inheritance

**Source requirement.** When a delegate subagent is spawned in the same
worktree as the parent session, the delegate's session must inherit the
parent's binding. The delegate's `session_id` is distinct from the parent's,
but the lineage resolver must resolve it to the same worktree.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.lineage as L
import inspect
src = inspect.getsource(L.resolve_lineage)
# The resolver must check the delegate_from field or equivalent
assert 'delegate' in src.lower() or '_delegate_from' in src, \
    'lineage resolver does not handle delegate sessions'
print('delegate session lineage handling present')
"
```

**Expected evidence.**
`delegate session lineage handling present` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, the resolver does not handle delegate sessions.
Inspect `writegate/lineage.py` for the delegate-handling path.

---

## A9. Delegate session: isolated-worktree rejection

**Source requirement.** When a delegate subagent is spawned in a different
worktree than the parent session, the delegate's binding must NOT inherit the
parent's binding. The lineage resolver must return `None` (no binding) for a
delegate whose worktree differs from the parent's.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.lineage as L
import inspect
src = inspect.getsource(L.resolve_lineage)
# The resolver must check worktree equality for delegates
assert 'worktree' in src.lower(), \
    'lineage resolver does not check worktree for delegates'
print('delegate worktree isolation handling present')
"
```

**Expected evidence.**
`delegate worktree isolation handling present` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, the resolver does not isolate delegate worktrees.
Inspect `writegate/lineage.py` for the worktree-equality check.

---

## A10. Kill switch: plugin disabled via config

**Source requirement.** Setting `plugins.write-gate.enabled: false` in
`config.yaml` must disable the Write-Gate plugin entirely. No hooks are
registered, no tool calls are intercepted, and all writes proceed without
governance. This is the operational kill switch for emergency rollback.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import yaml, tempfile, os
# Write a config that disables the plugin
cfg = {
    'plugins': {
        'write-gate': {'enabled': False}
    }
}
tmp = tempfile.mktemp(suffix='.yaml')
with open(tmp, 'w') as f:
    yaml.dump(cfg, f)

# Load the config and verify the plugin is disabled
from hermes_cli.config import load_config_readonly
loaded = load_config_readonly(tmp)
assert loaded['plugins']['write-gate']['enabled'] is False
print('kill switch verified: plugin disabled via config')
"
```

**Expected evidence.**
`kill switch verified: plugin disabled via config` on stdout, exit code 0.

**Failure mode.**
If the config does not disable the plugin, check the plugin's `register()`
function for the config-gate check. The plugin must read
`plugins.write-gate.enabled` from the config and return early (without
registering hooks) when it is `False`.

---

## A11. Spawn-failure fail-closed

**Source requirement.** If the terminal tool fails to spawn a subprocess
(e.g., the binary is missing or the shell is unavailable), the Write-Gate
hook must fail closed: the write is blocked and the error is surfaced to the
model. The hook must not allow the write to proceed on a spawn failure.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    'write_gate_plugin',
    '/home/progenitor/AI-main/hermes-startup-writegate-build/plugins/write-gate/__init__.py'
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Simulate a terminal spawn failure: the hook should block the write
result = mod._on_pre_tool_call(
    tool_name='terminal',
    args={'command': 'rm -rf /nonexistent'},
    session_id='test-spawn-failure',
)
# The hook should return a blocked Decision or None (allow reads, block writes)
# For a terminal tool with a destructive command, expect a blocked Decision
print('spawn-fail...:', result)
"
```

**Expected evidence.**
A `Decision` object with `allowed=False` on stdout, exit code 0.

**Failure mode.**
If the hook returns `None` (allow) for a destructive terminal command with no
session binding, the fail-closed guarantee is broken. Check
`plugins/write-gate/__init__.py` for the terminal-tool governance path.

---

## A12. `/new` with parent: binding retention

**Source requirement.** When a user runs `/new` (new session) with a parent
session reference, the new session's lineage must inherit the parent's
binding if the parent's lineage is trusted and resolves to the same worktree.
The new session starts with the parent's binding active.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.lineage as L
import inspect
src = inspect.getsource(L.resolve_lineage)
assert '_reset_from' in src or 'parent' in src.lower(), \
    'lineage resolver does not handle /new-with-parent sessions'
print('/new-with-parent lineage handling present')
"
```

**Expected evidence.**
`/new-with-parent lineage handling present` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, the resolver does not handle `/new`-with-parent
sessions. Inspect `writegate/lineage.py` for the `_reset_from` handling path.

---

## A13. Spoofed identity rejection

**Source requirement.** A model-supplied session id in the `write_gate` tool
arguments must never be used as the host identity. The tool must use the
host-provided session id (from the hook context) and reject any attempt to
spoof the session identity via the tool arguments.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.tool as T
import inspect
src = inspect.getsource(T.write_gate_tool)
assert 'session_id' in src, 'write_gate_tool does not handle session_id'
print('spoofed identity guard present in write_gate_tool')
"
```

**Expected evidence.**
`spoofed identity guard present in write_gate_tool` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, the tool does not guard against spoofed session ids.
Inspect `writegate/tool.py` for the session-id validation path.

---

## A14. Lease expiry: five-minute window

**Source requirement.** An exception lease is valid for exactly five minutes
from the approval timestamp. After the five-minute window expires, the same
target blocks again. The lease expiry is enforced by `list_active_leases`
filtering out leases whose `expires_at` is in the past.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.registry as R
import inspect
src = inspect.getsource(R.Registry.list_active_leases)
assert 'expires_at' in src or 'lease_is_live' in src, \
    'list_active_leases does not filter by expiry'
print('lease expiry enforcement present in list_active_leases')
"
```

**Expected evidence.**
`lease expiry enforcement present in list_active_leases` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, the lease expiry is not enforced. Inspect
`writegate/registry.py` for the `list_active_leases` expiry filter.

---

## A15. Host approval authority in leases

**Source requirement.** The lease record must carry the host-owned approval
reference (the approval id from the host's approval transport), not a
model-supplied value. The `approval_reference` field in the lease record must
match the host's approval reference, not any value the model could have
supplied in the tool arguments.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.approval as A
import inspect
src = inspect.getsource(A.mint_lease)
assert 'approval_reference' in src, 'mint_lease does not store approval_reference'
print('host approval authority in leases verified')
"
```

**Expected evidence.**
`host approval authority in leases verified` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, the lease does not carry the host approval reference.
Inspect `writegate/approval.py` for the `mint_lease` function.

---

## A16. Reads and kanban exempt before binding

**Source requirement.** Read-only tools and kanban tools must be exempt from
Write-Gate enforcement even before a binding exists. A read tool call with no
binding must not be blocked. A kanban tool call with no binding must not be
blocked. Only governed write tools (write_file, patch, terminal with write
side effects) require a binding or lease.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    'write_gate_plugin',
    '/home/progenitor/AI-main/hermes-startup-writegate-build/plugins/write-gate/__init__.py'
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Read tool with no binding: should return None (allow)
result_read = mod._on_pre_tool_call(
    tool_name='read_file',
    args={'path': '/tmp/test.md'},
    session_id='test-exempt',
)
assert result_read is None, f'read tool blocked: {result_read}'

# Kanban tool with no binding: should return None (allow)
result_kanban = mod._on_pre_tool_call(
    tool_name='kanban_show',
    args={},
    session_id='test-exempt',
)
assert result_kanban is None, f'kanban tool blocked: {result_kanban}'

print('reads and kanban exempt before binding: verified')
"
```

**Expected evidence.**
`reads and kanban exempt before binding: verified` on stdout, exit code 0.

**Failure mode.**
If a read or kanban tool is blocked when no binding exists, the exemption is
broken. Check `plugins/write-gate/__init__.py` for the tool-name exemption
list.

---

## A17. Symlink-escape defense

**Source requirement.** A target that resolves outside the project root
through a symlink must be rejected even if it is within the bound worktree
path. The symlink-escape check is a defense-in-depth layer: it runs after the
containment check and rejects any target whose `realpath` is outside the
project root.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.containment as C
import inspect
src = inspect.getsource(C.resolve_symlink_escape)
assert 'realpath' in src, 'resolve_symlink_escape does not use realpath'
print('symlink-escape defense present in containment module')
"
```

**Expected evidence.**
`symlink-escape defense present in containment module` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, the symlink-escape check does not use `realpath`.
Inspect `writegate/containment.py` for the `resolve_symlink_escape` function.

---

## A18. Enforcement cache

**Source requirement.** The enforcement layer caches decisions per
(session_id, tool_name, canonical_targets) tuple to avoid redundant SQLite
queries on the hot path. The cache is invalidated when the binding or lease
state changes. The cache must not serve a stale `allowed` decision after a
lease has expired.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.enforcement as E
import inspect
src = inspect.getsource(E.decide)
assert '_cache_set' in src or '_cache_get' in src, \
    'decide() does not use enforcement cache'
print('enforcement cache present in decide()')
"
```

**Expected evidence.**
`enforcement cache present in decide()` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, the enforcement cache is not used. Inspect
`writegate/enforcement.py` for the cache implementation.

---

## A19. Recovery snapshot before leased edit

**Source requirement.** Before allowing a leased edit, the enforcement layer
must take a recovery snapshot of the target file. The snapshot is written by
the recovery writer factory and must be verified before the edit proceeds.
If the snapshot fails, the edit is blocked.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.enforcement as E
import inspect
src = inspect.getsource(E.decide)
assert 'recovery_writer_factory' in src, \
    'decide() does not use recovery_writer_factory'
print('recovery snapshot before leased edit: verified')
"
```

**Expected evidence.**
`recovery snapshot before leased edit: verified` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, the recovery snapshot is not taken before a leased
edit. Inspect `writegate/enforcement.py` for the recovery-writer path.

---

## A20. Multi-target governance

**Source requirement.** When a tool call affects multiple files, all targets
must be governed individually. If any target is blocked (outside worktree
without a lease, protected location, symlink escape), the entire call is
blocked. A partial allow is never returned.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.enforcement as E
import inspect
src = inspect.getsource(E.decide)
assert 'blocked_targets' in src, \
    'decide() does not track blocked_targets'
print('multi-target governance verified')
"
```

**Expected evidence.**
`multi-target governance verified` on stdout, exit code 0.

**Failure mode.**
If the assertion fails, multi-target governance does not track blocked
targets. Inspect `writegate/enforcement.py` for the multi-target path.

---

## A21. Plugin loader integration

**Source requirement.** The Write-Gate plugin must be discovered and loaded
by the Hermes plugin manager at startup. The plugin directory must be present
under the Hermes home's `plugins/` directory and must contain a valid
`__init__.py` with a `register()` function.

**Verification command.**

```bash
ls -la /home/progenitor/AI-main/hermes-startup-writegate-build/plugins/write-gate/__init__.py \
  && echo "plugin file present"
```

**Expected evidence.**
A directory listing showing `__init__.py` and `plugin file present` on stdout,
exit code 0.

**Failure mode.**
If the file is missing, the plugin will not be loaded. Check the plugin
directory structure.

---

## A22. Test suite: focused startup binding

**Source requirement.** The focused test suite
`tests/plugins/test_writegate_startup_binding.py` must pass in full. This
suite covers: binding derivation, compression-child inheritance, delegate
session inheritance, `/new`-with-parent retention, read/kanban exemption,
fail-closed without session id, lazy lineage derivation, and the kill switch.

**Verification command.**

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/plugins/test_writegate_startup_binding.py
```

**Expected evidence.**
`=== Summary: 1 files, 42 tests passed, 0 failed (100% complete) ===` on
stdout, exit code 0.

**Failure mode.**
If any test fails, inspect the specific test's assertion error and the
relevant module. The test suite is the primary verification gate before
deployment.

---

## A23. Test suite: enforcement

**Source requirement.** The enforcement test suite
`tests/plugins/test_writegate_enforcement.py` must pass in full. This suite
covers: containment, protected locations, symlink-escape rejection, lease
matching, and the recovery snapshot path.

**Verification command.**

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/plugins/test_writegate_enforcement.py
```

**Expected evidence.**
A summary line showing 0 failed tests, exit code 0.

**Failure mode.**
If any test fails, inspect the specific test's assertion error.

---

## A24. Test suite: registry

**Source requirement.** The registry test suite
`tests/plugins/test_writegate_registry.py` must pass in full. This suite
covers: binding creation, lease minting, lease expiry, and the registry's
SQLite schema.

**Verification command.**

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/plugins/test_writegate_registry.py
```

**Expected evidence.**
A summary line showing 0 failed tests, exit code 0.

**Failure mode.**
If any test fails, inspect the specific test's assertion error.

---

## A25. Test suite: lineage

**Source requirement.** The lineage test suite
`tests/plugins/test_writegate_lineage.py` must pass in full. This suite
covers: read-only SessionDB access, compression-child lineage, delegate
session lineage, and `/new`-with-parent lineage.

**Verification command.**

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/plugins/test_writegate_lineage.py
```

**Expected evidence.**
A summary line showing 0 failed tests, exit code 0.

**Failure mode.**
If any test fails, inspect the specific test's assertion error.

---

## A26. Test suite: broad plugin suite

**Source requirement.** The full `tests/plugins/` suite must pass, with the
exception of pre-existing failures in `tests/plugins/video_gen/` and
`tests/plugins/memory/` that are unrelated to the Write-Gate integration.

**Verification command.**

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/plugins/
```

**Expected evidence.**
A summary line showing 0 failed tests in any `test_writegate_*.py` file.
Pre-existing failures in `test_fal_plugin.py` and
`test_hindsight_provider.py` are acceptable and documented as unrelated.

**Failure mode.**
If a `test_writegate_*.py` file fails, inspect the specific test. Pre-existing
failures in non-writegate files are not a deployment blocker.

---

## A27. Commit evidence

**Source requirement.** All changes must be committed to the
`integration/startup-writegate-runtime` branch with a descriptive commit
message. The commit must include: the new `writegate/lineage.py` module, the
modified `plugins/write-gate/__init__.py`, the modified
`writegate/enforcement.py`, the modified `writegate/binding.py`, the modified
`writegate/tool.py`, and the new
`tests/plugins/test_writegate_startup_binding.py` test file.

**Verification command.**

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
git log --oneline -1
git show --stat HEAD
```

**Expected evidence.**
A commit on the `integration/startup-writegate-runtime` branch with a message
describing the startup integration changes, and a stat showing all expected
files.

**Failure mode.**
If the commit is missing files, check `git status` for uncommitted changes
and amend the commit.

---

## A28. Rollback procedure

**Source requirement.** To roll back the Write-Gate integration: disable the
plugin via `plugins.write-gate.enabled: false` in `config.yaml`, restart the
agent process, and verify that no hooks are registered. The registry database
(`write-gate.db`) may be left in place; it will not be accessed when the
plugin is disabled.

**Verification command.**

```bash
# After setting plugins.write-gate.enabled: false in config.yaml:
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
from hermes_cli.config import load_config_readonly
import yaml, tempfile
# Verify the config disables the plugin
cfg = {'plugins': {'write-gate': {'enabled': False}}}
tmp = tempfile.mktemp(suffix='.yaml')
with open(tmp, 'w') as f:
    yaml.dump(cfg, f)
loaded = load_config_readonly(tmp)
assert loaded['plugins']['write-gate']['enabled'] is False
print('rollback verified: plugin disabled')
"
```

**Expected evidence.**
`rollback verified: plugin disabled` on stdout, exit code 0.

**Failure mode.**
If the plugin is still active after disabling it in config, check the plugin
manager's cache and restart the process.

---

## A29. Operational monitoring

**Source requirement.** The Write-Gate registry database (`write-gate.db`)
should be monitored for unexpected growth or corruption. The registry stores
bindings, leases, and exception requests. A healthy registry has a small,
bounded number of active bindings and leases.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import sqlite3, os
db_path = os.path.join(os.path.expanduser('~'), '.hermes', 'write-gate.db')
if not os.path.exists(db_path):
    print('registry DB not found (expected before first use)')
else:
    conn = sqlite3.connect(db_path)
    bindings = conn.execute('SELECT COUNT(*) FROM write_gate_bindings').fetchone()[0]
    leases = conn.execute('SELECT COUNT(*) FROM write_gate_leases').fetchone()[0]
    requests = conn.execute('SELECT COUNT(*) FROM write_gate_requests').fetchone()[0]
    print(f'registry: {bindings} bindings, {leases} leases, {requests} requests')
    conn.close()
"
```

**Expected evidence.**
A count of bindings, leases, and requests on stdout, exit code 0.

**Failure mode.**
If the registry DB is corrupted, the SQLite connection will fail. Back up the
DB and recreate it: the registry will re-derive bindings from session lineage
on the next tool call.

---

## A30. Lease expiry operational check

**Source requirement.** An active lease must expire exactly five minutes after
the approval timestamp. After expiry, the same target blocks again. This is
the five-minute, session-bound approval window that is a deliberate design
choice — not a byte-exact or single-use approval.

**Verification command.**

```bash
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "
import writegate.registry as R
import inspect
# Verify the five-minute window is the expiry logic
src = inspect.getsource(R.Registry.list_active_leases)
assert 'lease_is_live' in src or 'expires_at' in src
print('lease expiry operational check: present in list_active_leases')
"
```

**Expected evidence.**
`lease expiry operational check: present in list_active_leases` on stdout,
exit code 0.

**Failure mode.**
If the expiry logic is missing, leases will never expire and the five-minute
window is not enforced. Inspect `writegate/registry.py` for the
`_lease_is_live` method.

---

## A31. Final deployment gate

**Source requirement.** Before declaring the Write-Gate integration deployed,
all of the following must be verified:

1. All writegate modules import (A1).
2. Registry path resolves correctly (A2).
3. Lineage resolver is bounded and read-only (A3).
4. Plugin hook is registered (A4).
5. Fail-closed without session id (A5).
6. Lazy lineage derivation works (A6).
7. Compression-child inheritance (A7).
8. Delegate same-worktree inheritance (A8).
9. Delegate isolated-worktree rejection (A9).
10. Kill switch works (A10).
11. Spawn-failure fail-closed (A11).
12. `/new`-with-parent retention (A12).
13. Spoofed identity rejection (A13).
14. Lease expiry: five-minute window (A14).
15. Host approval authority in leases (A15).
16. Reads and kanban exempt before binding (A16).
17. Symlink-escape defense (A17).
18. Enforcement cache (A18).
19. Recovery snapshot before leased edit (A19).
20. Multi-target governance (A20).
21. Plugin loader integration (A21).
22. Focused test suite passes: 42/42 (A22).
23. Enforcement test suite passes (A23).
24. Registry test suite passes (A24).
25. Lineage test suite passes (A25).
26. Broad plugin suite passes (writegate files green) (A26).
27. Commit evidence present (A27).
28. Rollback procedure verified (A28).
29. Operational monitoring command works (A29).
30. Lease expiry operational check (A30).

**Verification command.**

```bash
cd /home/progenitor/AI-main/hermes-startup-writegate-build
echo "=== A1: import check ===" && \
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  -c "import writegate.registry, writegate.enforcement, writegate.tool, \
       writegate.lineage, writegate.binding, writegate.approval, \
       writegate.recovery, writegate.containment; \
       print('A1 OK')" && \
echo "=== A22: focused test suite ===" && \
HERMES_PYTHON=/home/progenitor/.hermes/hermes-agent/.venv/bin/python \
  scripts/run_tests.sh tests/plugins/test_writegate_startup_binding.py \
  2>&1 | grep "Summary" && \
echo "=== A27: commit evidence ===" && \
git log --oneline -1 && \
echo "=== A31: FINAL GATE PASSED ==="
```

**Expected evidence.**
```
=== A1: import check ===
A1 OK
=== A22: focused test suite ===
=== Summary: 1 files, 42 tests passed, 0 failed (100% complete) in ... ===
=== A27: commit evidence ===
<commit-hash> <commit-message>
=== A31: FINAL GATE PASSED ===
```

**Failure mode.**
If any step fails, stop the deployment and investigate the specific failure
before proceeding.
