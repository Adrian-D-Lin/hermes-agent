"""``pre_tool_call`` enforcement decision for WriteGate.

This is the ``§5 governed-route enforcement`` core.  The plugin registers
``_on_pre_tool_call`` (see ``__init__``) as a ``pre_tool_call`` hook; the hook
calls :func:`decide` and returns a ``block`` directive when the mutation must
be rejected.

Classification (always-allow / govern / block):

* **Always allow** — ``read_file``, ``search_files``, the ``write_gate``
  control tool, and the *exact* recognized Kanban model-tool operations from
  ``Canon/kanban-recognized-operations.md`` (no ``startswith("kanban_")``
  wildcard — ``§6`` / A29).
* **Govern** — ``write_file``, ``patch`` replace mode, every ``patch``
  multi-file/V4A target (add/update/delete/move), and obvious direct terminal
  filesystem mutations (best-effort: both source and destination for
  move/rename).

For each resolved target:

* canonicalize with real-path/parent resolution appropriate to existing and
  not-yet-existing targets;
* reject traversal and symlink escape; never use string-prefix containment;
* no binding -> block with structured remediation;
* inside the exact bound worktree and not protected -> allowed without lease;
* ``Canon/**``, ``4-artifacts/**``, ``5-archive/**``, or outside the bound
  worktree -> matching active lease required;
* multiple targets -> authority/recovery for *every* target before any
  execution; one failing target blocks the whole tool call;
* registry/policy/recovery errors fail closed.

``§7`` assurance boundary: arbitrary shells / interpreters / subprocesses /
plugins / MCP tools are **not** a comprehensive filesystem boundary.  Obvious
terminal mutations may be screened, but total containment is never advertised
and read-only terminal work is never globally disabled.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import binding as _binding
from . import recovery as _recovery
from . import registry as _registry
from .containment import canonicalize_target, is_within, resolve_symlink_escape


# The exact recognized Kanban model-tool operations (Canon / §6).  No prefix
# wildcard: ``kanban_xxx`` that is not on this list is NOT authority.
RECOGNIZED_KANBAN_TOOLS = frozenset({
    "kanban_create", "kanban_complete", "kanban_block", "kanban_unblock",
    "kanban_comment", "kanban_link", "kanban_heartbeat", "kanban_attach",
    "kanban_attach_url", "kanban_attachments", "kanban_show", "kanban_list",
    "kanban_request_changes", "kanban_request_review",
})

# Control tool is always allowed (it drives confirm_binding / request_exception).
CONTROL_TOOL = "write_gate"

# Tools that perform a persistent mutation and are therefore governed.
GOVERNED_TOOLS = frozenset({"write_file", "patch"})

# Protected project-root subdirectories that require a lease even inside a
# bound worktree (§4 / §5).
PROTECTED_PREFIXES = ("Canon/", "4-artifacts/", "5-archive/")

# Model-visible closing line on every block message: the assurance boundary
# (§7) is not a comprehensive filesystem boundary, so the model must be told
# explicitly that a block is not a cue to route the same mutation through some
# other surface.
ALTERNATE_ROUTE_PROHIBITION = (
    "Do not retry through terminal, execute_code, Python or shell scripts, "
    "plugins, MCP tools, or another alternate write route."
)


def format_block_message(reason: str, remediation: str = "") -> str:
    """Build the single model-visible block message from ``reason`` plus an
    optional ``remediation``.

    Always appends :data:`ALTERNATE_ROUTE_PROHIBITION` last, so every block —
    whether or not a remediation is available — closes with the same
    no-alternate-route instruction the model must not skip.
    """
    parts = [reason]
    if remediation:
        parts.append(f"Required next action: {remediation}")
    parts.append(ALTERNATE_ROUTE_PROHIBITION)
    return "\n\n".join(parts)


@dataclass
class Decision:
    """The outcome of classifying one governed tool call."""

    allowed: bool
    reason: str = ""
    # When the call is governed but needs a lease, carry the derived data so the
    # caller (plugin hook) can build the structured remediation.
    blocked_targets: List[str] = None
    remediation: str = ""
    # True when this allow was granted via a matching active lease (an
    # out-of-worktree / protected-location write).  A lease-authorized allow is
    # deliberately never cached: the hot-path cache must not let a stale hit
    # bypass the exact lease expiry or the mandatory recovery re-check on
    # retry.  Only ordinary, in-worktree, non-protected decisions are cached.
    leased: bool = False

    def to_block(self) -> Optional[Dict[str, str]]:
        if self.allowed:
            return None
        return {
            "action": "block",
            "message": format_block_message(self.reason, self.remediation),
        }


def is_recognized_kanban(tool_name: str) -> bool:
    """A tool is an exempt Kanban control-plane op only if it is on the exact
    canonical list (A29).  ``kanban_foo`` not on the list -> False.
    """
    return tool_name in RECOGNIZED_KANBAN_TOOLS


def is_always_allowed(tool_name: str) -> bool:
    if tool_name in ("read_file", "search_files"):
        return True
    if tool_name == CONTROL_TOOL:
        return True
    return is_recognized_kanban(tool_name)


def _targets_from_args(tool_name: str, args: Any) -> List[str]:
    """Extract the filesystem target(s) a governed tool call mutates.

    Returns an empty list for tools we do not govern.  For ``patch`` V4A
    multi-file mode every header path is a target.
    """
    if not isinstance(args, dict):
        return []
    targets: List[str] = []
    if tool_name == "write_file":
        if args.get("path"):
            targets.append(str(args["path"]))
    elif tool_name == "patch":
        # Replace mode: the explicit path.
        if args.get("path"):
            targets.append(str(args["path"]))
        # V4A multi-file: parse ``patch`` content headers.
        patch_text = args.get("patch")
        if isinstance(patch_text, str) and patch_text:
            targets.extend(_parse_v4a_paths(patch_text))
    return targets


def _parse_v4a_paths(patch_text: str) -> List[str]:
    """Extract ``*** Add File:`` / ``*** Update File:`` / ``*** Move File:``
    header paths from a V4A patch body.  Best-effort; malformed headers are
    ignored (the governed tool still executes, but the gate only screens what
    it can classify).
    """
    paths: List[str] = []
    for line in patch_text.splitlines():
        line = line.strip()
        if line.startswith("***"):
            # ``*** Update File: path`` or ``*** Move File: src -> dst``.
            body = line[len("***"):].strip()
            if body.startswith("Move File:"):
                rest = body[len("Move File:"):].strip()
                if " -> " in rest:
                    src = rest.split(" -> ", 1)[0].strip()
                    dst = rest.split(" -> ", 1)[1].strip()
                    # A Move governs BOTH endpoints: the source is deleted and
                    # the destination is created, so both must be screened.
                    if src:
                        paths.append(src)
                    if dst:
                        paths.append(dst)
                continue
            if ":" in body:
                p = body.split(":", 1)[1].strip()
                if p:
                    paths.append(p)
    return paths


def _terminal_fs_mutations(tool_name: str, args: Any) -> List[str]:
    """Best-effort: detect obvious direct filesystem mutations in a terminal
    command (rename/move/cp/mv/rm/touch/mkdir).  Returns target paths.
    """
    if not isinstance(args, dict):
        return []
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return []
    tokens = command.split()
    if not tokens:
        return []
    verb = tokens[0].split("/")[-1]
    targets: List[str] = []
    # rename/mv/cp/rm/mkdir/touch take path arguments; we screen only the
    # obvious ones and only when they look like paths.
    if verb in {"mv", "cp", "rm", "mkdir", "touch", "rename"}:
        for tok in tokens[1:]:
            if tok.startswith(("/", "~")) or "." in tok.split("/")[-1]:
                targets.append(tok)
    elif verb in {"ln"}:
        # ln -s target linkname  (only screen when a path-like target is present)
        for tok in tokens[1:]:
            if tok.startswith(("/", "~")):
                targets.append(tok)
    return targets


def decide(
    *,
    tool_name: str,
    args: Any,
    session_id: str,
    reg: _registry.Registry,
    project_root: Optional[str] = None,
    recovery_writer_factory=None,
    now_iso: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> Decision:
    """Classify a governed tool call and return a :class:`Decision`.

    ``project_root`` is the Git project root derived from the bound worktree
    (used to resolve ``Canon/**`` / ``4-artifacts/**`` / ``5-archive/**``).
    ``recovery_writer_factory`` is an optional callable
    ``(worktree, session_id, lease_id) -> RecoveryWriter`` used by the
    leased path to snapshot evidence before allowing execution.
    ``base_dir`` is the trusted per-session/task directory the file tool
    resolves relative targets against (``os.getcwd()`` when omitted); relative
    targets are resolved against it, never against the enforcement process cwd.
    """
    # 1. Always-allow path (reads, control tool, exact Kanban ops).
    if is_always_allowed(tool_name):
        return Decision(allowed=True)

    # 2. Determine whether this tool performs a governed mutation.
    targets = _targets_from_args(tool_name, args)
    if not targets:
        # Terminal best-effort screening: only govern obvious mutations.
        targets = _terminal_fs_mutations(tool_name, args)
    if not targets:
        # Not a governed route we can classify -> allow (assurance boundary
        # §7: we do not claim total containment, and we never disable
        # read-only terminal work).
        return Decision(allowed=True)

    # 2b. Enforcement hot path: the pre_tool_call hook fires on every governed
    #      tool call, so the binding + lease lookup is cached.  The cache is
    #      keyed on (session_id, tool_name, sorted *canonical* target set) and
    #      invalidated the instant the registry mutates (see registry.py
    #      `_cache_clear`).  TTL is short and conservative — the 5-minute lease
    #      window is enforced by the registry's own timestamps, not the cache.
    #      This keeps the hot path well under 100 ms.
    #
    #      Cache keying note: the key is built from the *canonical resolved*
    #      targets, not the raw strings.  A relative target such as
    #      ``Canon/foo.md`` must resolve against the same trusted per-session
    #      base directory on every call, so two calls with the same relative
    #      name that resolve to different absolute paths (e.g. after a cwd
    #      change) never collide in the cache.  A lease-authorized allow is
    #      deliberately NOT cached: caching it would bypass both the exact
    #      lease expiry and the mandatory recovery re-check on retry.
    canonical_targets = _canonical_target_set(targets, base_dir)
    cached = _cache_get(session_id, tool_name, canonical_targets)
    if cached is not None:
        return cached

    # 3. Resolve the session's binding.  No binding -> fail closed.
    binding = reg.get_active_binding(session_id)
    if binding is None or not binding.worktree_path:
        return Decision(
            allowed=False,
            reason=(
                "Write-Gate: no confirmed worktree binding for this session. "
                "Governed writes are blocked until the startup binding is "
                "confirmed via write_gate(action='confirm_binding'). Reads are "
                "still allowed."
            ),
            blocked_targets=targets,
            remediation=(
                "Call write_gate(action='confirm_binding') to re-anchor the "
                "session's worktree binding. Reads remain allowed while this "
                "is pending."
            ),
        )

    worktree = binding.worktree_path

    # 4. Evaluate every target.  All must pass; one failure blocks the call.
    #    Relative targets resolve against the trusted per-session/task base
    #    directory (``base_dir``), never the enforcement process cwd, so a
    #    cwd change between calls cannot silently redirect a governed write.
    leased_call = False
    for t in targets:
        canonical = canonicalize_target(t, base_dir=base_dir)
        if canonical is None:
            return _traversal_or_unresolvable(t)
        # Containment within the bound worktree.
        within = is_within(canonical, worktree)
        # Protected-location check (relative to the project root).
        protected = _is_protected(canonical, project_root)
        # Out-of-worktree or protected: a matching active lease is required.
        # A matching active lease is the human's folder-scoped authority for
        # the target (a five-minute, folder-scoped approval), and the
        # symlink-escape check is deliberately evaluated against the
        # *project root*, the only boundary a lease can legitimately extend
        # beyond the bound worktree.  An out-of-worktree target authorized by
        # a matching lease is *not* a symlink escape; an in-worktree target
        # that escapes the project root through a symlink still is.
        if not within or protected:
            leased_call = True
            lease = _matching_lease(reg, session_id=session_id, worktree=worktree,
                                    target=canonical, project_root=project_root)
            if lease is None:
                return Decision(
                    allowed=False,
                    reason=(
                        f"Write-Gate: {canonical!r} is outside the bound worktree "
                        f"{worktree!r}" + (
                            f" (protected location) " if protected else " "
                        ) + "and has no matching active lease."
                    ),
                    blocked_targets=[t],
                    remediation=(
                        "Call write_gate(action='request_exception') with "
                        "affected_files and stated_outcome for this target, "
                        "then wait for human approval. Once approved, retry "
                        "with write_file or patch."
                    ),
                )
            # Leased path: snapshot evidence immediately for **every** governed
            # target before allowing execution; the whole call fails if any
            # target's recovery evidence cannot be written or verified.  Retries
            # are idempotent because each retry re-snapshots the same target.
            if recovery_writer_factory is not None:
                writer = recovery_writer_factory(worktree, session_id, lease.lease_id)
                outcome = _recovery_for_target(writer, t, canonical)
                if not outcome.ok:
                    return Decision(
                        allowed=False,
                        reason=f"Write-Gate: recovery evidence could not be written or verified: {outcome.error}",
                        blocked_targets=[t],
                        remediation=(
                            "Stop and report the recovery-evidence failure to "
                            "the user/operator; retry only after the recovery "
                            "writer's worktree access or storage issue has "
                            "been resolved."
                        ),
                    )
            continue
        # Ordinary in-worktree write: allowed without a lease (§4).  Symlink
        # escapes (a target that resolves outside the project root through a
        # symlink) are still rejected in depth — a lease cannot authorize them.
        esc = resolve_symlink_escape(canonical, project_root) if project_root else False
        if esc:
            return _traversal_or_unresolvable(t)
    decision = Decision(allowed=True, leased=leased_call)
    _cache_set(session_id, tool_name, canonical_targets, decision)
    return decision


def _traversal_or_unresolvable(target: str) -> Decision:
    from .containment import has_traversal_component
    if has_traversal_component(target):
        reason = f"Write-Gate: path traversal rejected for {target!r}"
        remediation = (
            "Resend the request with a normalized path containing no '..' "
            "traversal segments, addressed to a location inside the bound "
            "worktree (or a leased folder for a protected/out-of-worktree "
            "target)."
        )
    else:
        reason = f"Write-Gate: cannot resolve target path {target!r}"
        remediation = (
            "Resend the request with a valid, resolvable path — check for "
            "malformed path syntax or an unresolvable/missing parent "
            "directory."
        )
    return Decision(
        allowed=False,
        reason=reason,
        blocked_targets=[target],
        remediation=remediation,
    )


def _is_protected(canonical: str, project_root: Optional[str]) -> bool:
    if not project_root:
        return False
    root = canonicalize_target(project_root, must_exist=True)
    if root is None:
        return False
    # Component-aware containment: split on path separators so that
    # ``Canon/`` matches ``Canon/`` (the root itself) and any descendant
    # component, and never a string-prefix false match such as
    # ``CanonFoo`` or ``myCanon/bar``.
    def _components(path: str) -> List[str]:
        parts = []
        for seg in path.replace("\\", "/").split("/"):
            if seg and seg not in (".", ".."):
                parts.append(seg)
        return parts

    root_components = _components(root)
    canon_components = _components(canonical)
    # The protected check is relative to the project root: strip the root's
    # components off the front of the canonical target, then inspect the first
    # *remaining* component.  A target directly inside ``Canon/`` therefore has
    # ``Canon`` as its first relative component, while ``/some/other/Canon/x``
    # (Canon elsewhere on the tree) has a different first relative component.
    # ``Canon`` at the project root: the first relative component is one of the
    # protected prefixes (without its trailing slash), or a descendant of it.
    if len(canon_components) < len(root_components):
        return False
    relative_components = canon_components[len(root_components):]
    if not relative_components:
        # Target equals the project root itself: not a protected subdirectory.
        return False
    protected_names = {p.rstrip("/") for p in PROTECTED_PREFIXES}
    first_relative = relative_components[0]
    if first_relative in protected_names:
        return True
    return False


def _matching_lease(
    reg: _registry.Registry,
    *,
    session_id: str,
    worktree: str,
    target: str,
    project_root: Optional[str],
) -> Optional[_registry.LeaseRecord]:
    """Return a lease that (a) is active, (b) matches the session, (c) matches
    the confirmed worktree, and (d) contains the target within its approved
    folder (§9).  None otherwise.
    """
    for lease in reg.list_active_leases(session_id=session_id):
        # Exact canonical equality: the lease worktree must match the bound
        # worktree exactly, not merely contain it (a broader lease must not
        # accept a narrower target).
        if not lease.confirmed_worktree or not worktree:
            continue
        lease_ct = canonicalize_target(lease.confirmed_worktree)
        worktree_ct = canonicalize_target(worktree)
        if lease_ct is None or worktree_ct is None:
            continue
        if lease_ct != worktree_ct:
            continue
        if not lease.approved_folder:
            continue
        folder = canonicalize_target(lease.approved_folder, must_exist=False)
        tgt = canonicalize_target(target)
        if folder and tgt and is_within(tgt, folder):
            return lease
    return None


def _recovery_for_target(writer, target: str, canonical: str) -> "_recovery.RecoveryOutcome":
    # Decide modify vs create vs move based on existence.
    if os.path.islink(canonical) or (os.path.exists(canonical) and not os.path.isfile(canonical)):
        # Directory or symlink target: treat as create (absence marker if absent).
        return writer.snapshot_absent(target)
    if os.path.exists(canonical):
        if os.path.isfile(canonical):
            return writer.snapshot_existing_file(target)
        return writer.snapshot_absent(target)
    return writer.snapshot_absent(target)


# ---------------------------------------------------------------------------
# Enforcement cache (see decide()'s 2b hot-path note)
# ---------------------------------------------------------------------------
import threading
import time as _time

_CACHE_TTL_SECONDS = 5.0
_lock = threading.Lock()
_cache: dict = {}  # key -> (expiry_ts, decision_allowed, reason)


def _canonical_target_set(
    targets: Sequence[str],
    base_dir: Optional[str],
) -> Sequence[str]:
    """Return the set of *canonical resolved* target paths.

    The cache is keyed on these, not the raw strings, so two calls with the
    same relative name that resolve to different absolute paths (e.g. after a
    cwd change, or against a different per-session ``base_dir``) never share a
    cache entry.  Targets that fail to resolve are dropped from the key; the
    caller re-evaluates them on every call and cannot be served a stale hit.
    """
    resolved = set()
    for t in targets:
        if not t:
            continue
        canonical = canonicalize_target(t, base_dir=base_dir)
        if canonical is not None:
            resolved.add(canonical)
    return sorted(resolved)


def _cache_key(session_id: str, tool_name: str, canonical_targets: Sequence[str]) -> str:
    return f"{session_id}|{tool_name}|{','.join(sorted(canonical_targets))}"


def _cache_get(session_id: str, tool_name: str, canonical_targets: Sequence[str]):
    key = _cache_key(session_id, tool_name, canonical_targets)
    with _lock:
        entry = _cache.get(key)
    if entry is None:
        return None
    expiry, allowed, reason = entry
    if _time.monotonic() > expiry:
        with _lock:
            _cache.pop(key, None)
        return None
    return Decision(allowed=allowed, reason=reason)


def _cache_set(
    session_id: str,
    tool_name: str,
    canonical_targets: Sequence[str],
    decision: "Decision",
) -> None:
    if not canonical_targets:
        return
    # Never cache a lease-authorized allow.  A lease is time-boxed and each
    # retry must re-check expiry and re-run recovery; caching the allow would
    # bypass both.  Only ordinary, in-worktree, non-protected decisions are
    # cached on the hot path.
    if decision.leased:
        return
    key = _cache_key(session_id, tool_name, canonical_targets)
    expiry = _time.monotonic() + _CACHE_TTL_SECONDS
    with _lock:
        _cache[key] = (expiry, decision.allowed, decision.reason)


def _cache_clear() -> None:
    """Invalidates the enforcement cache on any registry mutation."""
    with _lock:
        _cache.clear()
