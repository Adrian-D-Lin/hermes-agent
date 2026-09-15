"""Durable repository-registry and Project-binding primitives (S2C2b).

Registry entry under ``kanban.repository_registry``::

    kanban.repository_registry[project_slug] = {
        "repository_root": "<canonical checkout path>",
        "github_repository": "<owner>/<repo>",
        "integration_branch": "<branch>",
    }

Two public operations:

* :func:`persist_repository_registry` — confirms the registry entry
  (exact-match / add-on-absent / actionable mismatch) and persists it via
  :func:`hermes_cli.config.save_config` with ``merge_existing=True``.
* :func:`persist_project_binding` — creates/confirms the Hermes Project
  binding via :mod:`hermes_cli.projects_db`, reusing that module rather
  than duplicating its schema.

Both operations take injected seam parameters (``config_getter`` /
``config_writer`` / ``projects_db_getter`` / ``projects_db_connector``) so
tests exercise temporary state; the defaults use the live Hermes config and
projects DB.

Error semantics:

* :class:`PersistenceError` — one actionable error naming the failed
  condition and a remediation. Expected operational failures
  (config/filesystem/SQLite) are wrapped.
* Programmer-control exceptions from injected seams (``AssertionError``,
  ``KeyError``, ``TypeError``, ``AttributeError``) propagate unchanged.

Identity fields (``canonical_checkout``, ``normalized_origin``,
``integration_branch``, ``controlled_worktree_root``, ``repository``) are
validated exactly: no trimming, no normalization, no expanduser.
``display_name`` is the one field that retains trimming via
:func:`_require_nonblank`.

The canonical checkout is validated against :data:`AI_MAIN_ROOT`
(``session_startup_proposal.AI_MAIN_ROOT``): it must be the exact direct child
path ``AI_MAIN_ROOT/<project_slug>``. It is a *distinct* root from
``controlled_worktree_root`` and is never validated as inside the controlled
worktree root.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import hermes_cli.config as _config
import hermes_cli.projects_db as _projects_db

from .session_startup_proposal import AI_MAIN_ROOT

__all__ = [
    "PersistenceError",
    "ProjectPersistenceResult",
    "RegistryPersistenceResult",
    "persist_project_binding",
    "persist_repository_registry",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CANONICAL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")
_KANBAN_KEY = "kanban"
_REGISTRY_KEY = "repository_registry"
_CONTROLLED_WORKTREE_ROOT_KEY = "controlled_worktree_root"
_REGISTRY_ENTRY_KEYS = ("repository_root", "github_repository", "integration_branch")


# ---------------------------------------------------------------------------
# Error and result types
# ---------------------------------------------------------------------------

class PersistenceError(Exception):
    """One actionable recoverable error for the persistence primitives.

    The message names the failed condition and a remediation.
    """


@dataclass(frozen=True)
class RegistryPersistenceResult:
    """Confirmed registry entry and its project slug."""

    project_slug: str
    registry_entry: Dict[str, str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "project_slug": self.project_slug,
            "registry_entry": dict(self.registry_entry),
        }


@dataclass(frozen=True)
class ProjectPersistenceResult:
    """Created or confirmed Hermes Project record fields."""

    project_id: str
    project_slug: str
    display_name: str
    primary_path: str
    board_slug: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_slug": self.project_slug,
            "display_name": self.display_name,
            "primary_path": self.primary_path,
            "board_slug": self.board_slug,
        }


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------

def _require_nonblank(value: Any, name: str) -> str:
    """Nonblank check with trimming; only ``display_name`` uses this."""
    if not isinstance(value, str) or not value.strip():
        raise PersistenceError(f"{name} must be a nonblank string")
    return value.strip()


def _require_exact_nonblank(value: Any, name: str) -> str:
    """Exact nonblank check: padded values (e.g. ``" main"``) are rejected,
    not trimmed."""
    if not isinstance(value, str) or not value.strip():
        raise PersistenceError(f"{name} must be a nonblank string")
    if value != value.strip():
        raise PersistenceError(
            f"{name} must not have leading or trailing whitespace; re-confirm "
            "the exact value"
        )
    return value


def _require_canonical_slug(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise PersistenceError(f"{name} must be a string")
    if not _CANONICAL_SLUG_RE.match(value):
        raise PersistenceError(
            f"{name} {value!r} is not in the canonical form accepted by the "
            "Project store; re-confirm the proposal so the slug matches the "
            "stored form"
        )
    return value


def _require_absolute_root(value: Any, name: str) -> str:
    """Exact nonblank check plus an absolute-path check; used for the
    controlled worktree root so a relative value is rejected before any
    containment or agreement logic."""
    if not isinstance(value, str) or not value.strip():
        raise PersistenceError(f"{name} must be a nonblank string")
    if value != value.strip():
        raise PersistenceError(
            f"{name} must not have leading or trailing whitespace; re-confirm "
            "the exact value"
        )
    if not os.path.isabs(value):
        raise PersistenceError(
            f"{name} {value!r} is not absolute; the controlled worktree root "
            "must be an absolute path"
        )
    return value


def _require_agreement(
    proposal: Dict[str, str],
    evidence: Dict[str, str],
) -> None:
    """Confirm the proposal and evidence agree exactly on the identity fields.

    Raises on the first mismatch, naming the field and the expected
    (evidence) value. ``display_name`` is proposal-only and excluded.
    """
    identity_fields = ("canonical_checkout", "repository", "integration_branch")
    evidence_map = {
        "canonical_checkout": evidence.get("canonical_checkout"),
        "repository": evidence.get("github_repository"),
        "integration_branch": evidence.get("integration_branch"),
    }
    mismatches = [
        field
        for field in identity_fields
        if proposal.get(field) != evidence_map[field]
    ]
    if mismatches:
        raise PersistenceError(
            "the confirmed proposal does not agree with the checkout evidence "
            f"on: {', '.join(mismatches)}; the proposal and evidence must "
            "match exactly before persistence — re-confirm the proposal "
            "against the established checkout, do not silently adopt "
            "evidence values"
        )


def _validate_proposal_mapping(proposal: Any) -> Dict[str, str]:
    """Return the confirmed proposal fields, raising on the first missing
    or malformed field."""
    if not isinstance(proposal, dict):
        raise PersistenceError(
            "the confirmed proposal is not a mapping; re-run the onboarding "
            "conversation to produce a fresh proposal"
        )
    missing = [
        key
        for key in (
            "project_slug", "display_name", "canonical_checkout",
            "repository", "board_slug", "integration_branch",
            "controlled_worktree_root",
        )
        if key not in proposal
    ]
    if missing:
        raise PersistenceError(
            "the confirmed proposal is missing required field(s): "
            f"{', '.join(missing)}; re-run the onboarding conversation"
        )
    project_slug = _require_exact_nonblank(proposal["project_slug"], "project_slug")
    _require_canonical_slug(project_slug, "project_slug")
    display_name = _require_nonblank(proposal["display_name"], "display_name")
    board_slug = _require_exact_nonblank(proposal["board_slug"], "board_slug")
    _require_canonical_slug(board_slug, "board_slug")
    _require_exact_nonblank(proposal["canonical_checkout"], "canonical_checkout")
    _require_exact_nonblank(proposal["repository"], "repository")
    _require_exact_nonblank(proposal["integration_branch"], "integration_branch")
    _require_exact_nonblank(proposal["controlled_worktree_root"], "controlled_worktree_root")
    return {
        "project_slug": project_slug,
        "display_name": display_name,
        "board_slug": board_slug,
        "canonical_checkout": proposal["canonical_checkout"],
        "repository": proposal["repository"],
        "integration_branch": proposal["integration_branch"],
        "controlled_worktree_root": proposal["controlled_worktree_root"],
    }


def _validate_checkout_evidence(evidence: Any) -> Dict[str, str]:
    """Return the checkout evidence identity fields, raising
    :class:`PersistenceError` on the first missing or malformed field."""
    if evidence is None:
        raise PersistenceError(
            "no checkout evidence is available; establish the canonical "
            "checkout before registering the repository"
        )
    if hasattr(evidence, "as_dict") and not isinstance(evidence, dict):
        mapping = evidence.as_dict()
    elif isinstance(evidence, dict):
        mapping = evidence
    else:
        raise PersistenceError(
            "checkout evidence is neither a CheckoutEvidence instance nor a mapping"
        )
    required = ("canonical_checkout", "normalized_origin", "integration_branch")
    missing = [key for key in required if key not in mapping]
    if missing:
        raise PersistenceError(
            f"checkout evidence is missing required field(s): {', '.join(missing)}; re-establish the canonical checkout"
        )
    return {
        "canonical_checkout": _require_exact_nonblank(
            mapping["canonical_checkout"], "canonical_checkout"
        ),
        "github_repository": _require_exact_nonblank(
            mapping["normalized_origin"], "normalized_origin"
        ),
        "integration_branch": _require_exact_nonblank(
            mapping["integration_branch"], "integration_branch"
        ),
    }


def _validate_registry_entry(entry: Dict[str, str]) -> None:
    """Require the entry to be exactly the three-field mapping with string
    values; rejects missing keys, extra keys, or non-string values."""
    for field in _REGISTRY_ENTRY_KEYS:
        if field not in entry:
            raise PersistenceError(
                f"the registry entry is missing required field {field!r}; "
                "the entry must carry repository_root, github_repository, "
                "and integration_branch"
            )
        value = entry[field]
        if not isinstance(value, str):
            raise PersistenceError(
                f"the registry entry field {field!r} must be a string, "
                f"got {type(value).__name__}"
            )
    extra = set(entry) - set(_REGISTRY_ENTRY_KEYS)
    if extra:
        raise PersistenceError(
            f"the registry entry has unexpected field(s) "
            f"{', '.join(sorted(extra))}; the entry must be exactly "
            "{repository_root, github_repository, integration_branch} with no "
            "extra fields"
        )


def _require_controlled_root_agreement(
    proposal: Dict[str, str], config: Dict[str, Any]
) -> str:
    """Confirm the proposal's controlled root equals the config's root
    (literal comparison: no expanduser/normpath). Returns the configured
    root."""
    kanban = config.get(_KANBAN_KEY)
    if not isinstance(kanban, dict):
        raise PersistenceError(
            "kanban config must be a mapping; verify the Hermes configuration"
        )
    config_root = kanban.get(_CONTROLLED_WORKTREE_ROOT_KEY)
    if not isinstance(config_root, str) or not config_root.strip():
        raise PersistenceError(
            "kanban.controlled_worktree_root must be nonblank; set the "
            "controlled worktree root before registering a repository"
        )
    if not os.path.isabs(config_root):
        raise PersistenceError(
            "kanban.controlled_worktree_root must be an absolute path; verify "
            "the Hermes configuration"
        )
    proposal_root = proposal["controlled_worktree_root"]
    if proposal_root != config_root:
        raise PersistenceError(
            "the proposal's controlled_worktree_root "
            f"{proposal_root!r} does not match the configured "
            f"kanban.controlled_worktree_root {config_root!r}; the "
            "controlled worktree root must not be created, replaced, or "
            "normalized — re-confirm the proposal against the configured root"
        )
    return config_root


def _require_aimain_checkout(
    canonical_checkout: str,
    project_slug: str,
) -> str:
    """Validate ``canonical_checkout`` as the exact AI-main child path.

    The canonical checkout is the trusted repository registry's
    ``repository_root``: a direct child of :data:`AI_MAIN_ROOT` derived from
    ``project_slug``. It is a *distinct* root from the controlled worktree
    root, so it must never be validated as inside ``controlled_worktree_root``.

    The invariant is literal identity: the stored checkout string must equal
    ``AI_MAIN_ROOT + os.sep + project_slug`` exactly. No normalization, no
    resolution, no silent modification. This single check rejects the root
    itself, nested paths, traversal sequences, wrong roots, and any checkout
    whose direct-child name differs from the validated project slug.
    """
    expected = AI_MAIN_ROOT + os.sep + project_slug
    if canonical_checkout != expected:
        raise PersistenceError(
            f"canonical_checkout {canonical_checkout!r} is not the exact "
            f"AI-main child path for project {project_slug!r} "
            f"({expected!r}); the checkout must live directly beneath "
            f"AI-main with the project slug as its name — re-confirm the "
            "proposal against the established checkout"
        )
    return canonical_checkout


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def persist_repository_registry(
    proposal: Any,
    evidence: Any,
    *,
    config_getter: Optional[Callable[[], Dict[str, Any]]] = None,
    config_writer: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> RegistryPersistenceResult:
    """Confirm the trusted repository registry entry and persist it.

    Validates that the confirmed proposal and the checkout evidence agree
    exactly on the identity fields; confirms the proposal's
    ``controlled_worktree_root`` equals the configured root; validates the
    registry entry shape. Adds the entry if absent; succeeds without
    rewriting if it already matches exactly; fails with one actionable
    error naming the differing field if any field differs.

    The write uses :func:`hermes_cli.config.save_config` with
    ``merge_existing=True``; the payload is a minimal partial
    (``{kanban: {repository_registry: {project_slug: entry}}}``) so
    unrelated config and sibling entries are preserved.
    """
    proposal_map = _validate_proposal_mapping(proposal)
    evidence_map = _validate_checkout_evidence(evidence)
    _require_agreement(proposal_map, evidence_map)
    entry = {
        "repository_root": evidence_map["canonical_checkout"],
        "github_repository": evidence_map["github_repository"],
        "integration_branch": evidence_map["integration_branch"],
    }
    _validate_registry_entry(entry)
    config_get = config_getter or _config.load_config
    try:
        config = config_get()
    except (OSError, ValueError, RuntimeError) as exc:
        raise PersistenceError(f"repository registry config read failed: {exc}") from exc
    if not isinstance(config, dict):
        raise PersistenceError(
            "the Hermes configuration is not a mapping; verify the "
            "configuration file"
        )
    _require_controlled_root_agreement(proposal_map, config)
    project_slug = proposal_map["project_slug"]
    entry_root = entry["repository_root"]
    # The registry's repository_root is the canonical checkout: the exact
    # AI-main child path for the project slug, a *distinct* root from the
    # controlled worktree root. It is validated as literal identity, never
    # as inside the controlled root.
    _require_aimain_checkout(entry_root, project_slug)
    kanban = config.get(_KANBAN_KEY)
    if not isinstance(kanban, dict):
        raise PersistenceError(
            "kanban config must be a mapping; verify the Hermes configuration"
        )
    registry = kanban.get(_REGISTRY_KEY)
    if registry is not None and not isinstance(registry, dict):
        raise PersistenceError(
            "kanban.repository_registry must be a mapping; verify the "
            "configuration file"
        )
    existing = registry.get(project_slug) if registry is not None else None
    if existing is not None:
        if not isinstance(existing, dict):
            raise PersistenceError(
                f"kanban.repository_registry.{project_slug} must be a mapping"
            )
        _validate_registry_entry(existing)
        for field in _REGISTRY_ENTRY_KEYS:
            if existing.get(field) != entry[field]:
                raise PersistenceError(
                    f"the repository_registry entry for {project_slug!r} "
                    f"differs on {field!r} (registry={existing.get(field)!r}, "
                    f"proposal/evidence={entry[field]!r}); the confirmed entry "
                    "must match exactly — update or remove the existing entry "
                    "before retrying"
                )
        return RegistryPersistenceResult(
            project_slug=project_slug, registry_entry=dict(entry)
        )
    partial = {_KANBAN_KEY: {_REGISTRY_KEY: {project_slug: dict(entry)}}}
    writer = config_writer or (
        lambda cfg: _config.save_config(cfg, merge_existing=True)
    )
    try:
        writer(partial)
    except (OSError, ValueError, RuntimeError) as exc:
        raise PersistenceError(f"repository registry persistence failed: {exc}") from exc
    return RegistryPersistenceResult(
        project_slug=project_slug, registry_entry=dict(entry)
    )


def _validate_project_fields(proposal: Dict[str, str]) -> Dict[str, str]:
    """Return the four confirmed Project binding fields; validates the
    controlled worktree root (absolute, exact) before ``canonical_checkout``.

    The controlled worktree root is validated as an absolute path first so a
    relative value fails on the controlled root, not the checkout. The
    ``canonical_checkout`` is then validated as the exact AI-main child path.

    The canonical checkout is the trusted registry's ``repository_root``: a
    direct child of :data:`AI_MAIN_ROOT`, a *distinct* root from the controlled
    worktree root. It is validated as the exact AI-main path, never as inside
    ``controlled_worktree_root``.
    """
    _require_absolute_root(
        proposal["controlled_worktree_root"], "controlled_worktree_root"
    )
    canonical_checkout = _require_aimain_checkout(
        proposal["canonical_checkout"],
        proposal["project_slug"],
    )
    return {
        "project_slug": proposal["project_slug"],
        "display_name": proposal["display_name"],
        "canonical_checkout": canonical_checkout,
        "board_slug": proposal["board_slug"],
    }


def persist_project_binding(
    proposal: Any,
    *,
    projects_db_getter: Optional[Callable[[], Any]] = None,
    projects_db_connector: Optional[Callable[[], Any]] = None,
) -> ProjectPersistenceResult:
    """Create/confirm the Hermes Project binding.

    The four confirmed fields are ``project_slug`` (name),
    ``display_name`` (display name), ``canonical_checkout`` (primary_path),
    and ``board_slug``.

    Retry semantics: an existing non-archived project with the exact slug
    and exact four fields returns the same id without mutation; a slug
    conflict, primary-path conflict, board-slug conflict, an archived
    same-slug record, or any partial field mismatch fails actionably.
    ``create_project`` must never silently auto-suffix the confirmed slug;
    the created record is verified exactly, and if the API returns a
    different slug the call fails rather than reporting success.

    ``projects_db_connector`` is a ``connect_closing``-style context-manager
    factory; ``projects_db_getter`` injects the module. Defaults use the
    live projects_db module.
    """
    proposal_map = _validate_proposal_mapping(proposal)
    fields = _validate_project_fields(proposal_map)
    getter = projects_db_getter or (lambda: _projects_db)
    db = getter()
    connector = projects_db_connector or _projects_db.connect_closing
    try:
        with connector() as conn:
            try:
                return _bind_project(conn, db, fields)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                raise PersistenceError(f"Project binding persistence failed: {exc}") from exc
    except (AssertionError, KeyError, TypeError, AttributeError):
        raise
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise PersistenceError(f"Project binding persistence failed: {exc}") from exc


def _bind_project(
    conn: Any,
    db: Any,
    fields: Dict[str, str],
) -> ProjectPersistenceResult:
    project_slug = fields["project_slug"]
    display_name = fields["display_name"]
    primary_path = fields["canonical_checkout"]
    board_slug = fields["board_slug"]

    existing = db.get_project(conn, project_slug)
    if existing is not None and not existing.archived:
        if (
            existing.name == display_name
            and (existing.primary_path or "") == primary_path
            and (existing.board_slug or "") == board_slug
        ):
            return ProjectPersistenceResult(
                project_id=existing.id,
                project_slug=project_slug,
                display_name=display_name,
                primary_path=primary_path,
                board_slug=board_slug,
            )
        raise PersistenceError(
            f"a non-archived project '{existing.id}' already uses slug "
            f"{project_slug!r} with different fields (name={existing.name!r}, "
            f"primary_path={existing.primary_path!r}, board_slug="
            f"{existing.board_slug!r}); the confirmed Project binding must match "
            "exactly — update or archive the existing project before retrying"
        )

    if existing is not None and existing.archived:
        raise PersistenceError(
            f"an archived project '{existing.id}' already uses slug "
            f"{project_slug!r}; do not restore or overwrite archived projects — "
            "choose a different project slug"
        )

    for candidate in db.list_projects(conn, include_archived=False):
        if candidate.slug == project_slug:
            raise PersistenceError(
                f"a non-archived project '{candidate.id}' already uses slug "
                f"{project_slug!r}; choose a different project slug"
            )

    path_conflict = db.find_by_primary_path(conn, primary_path)
    if path_conflict is not None and not path_conflict.archived:
        raise PersistenceError(
            f"the canonical_checkout {primary_path!r} already belongs to "
            f"project '{path_conflict.id}'; switch to it instead of creating a "
            "duplicate"
        )

    if board_slug:
        for candidate in db.list_projects(conn, include_archived=False):
            if (candidate.board_slug or "") == board_slug and (
                candidate.primary_path or ""
            ) != primary_path:
                raise PersistenceError(
                    f"the board {board_slug!r} is already bound to project "
                    f"'{candidate.id}' (primary_path={candidate.primary_path!r}); "
                    "the confirmed board_slug must not be reused by a different "
                    "project"
                )

    created_id = db.create_project(
        conn,
        name=display_name,
        slug=project_slug,
        primary_path=primary_path,
        board_slug=board_slug,
    )

    created = db.get_project(conn, created_id)
    if created is None:
        raise PersistenceError(
            "the Project binding was created but the record is not readable; "
            "verify the Project database and retry"
        )
    if created.slug != project_slug:
        raise PersistenceError(
            f"the Project binding was created with slug {created.slug!r} but "
            f"the confirmed slug is {project_slug!r}; the slug was auto-suffixed "
            "and the confirmed slug could not be honored — retry after "
            "resolving the slug conflict"
        )
    if (
        created.name != display_name
        or (created.primary_path or "") != primary_path
        or (created.board_slug or "") != board_slug
    ):
        raise PersistenceError(
            "the created Project record does not match the confirmed fields "
            "(name, primary_path, board_slug); retry after verifying the "
            "Project database"
        )

    return ProjectPersistenceResult(
        project_id=created.id,
        project_slug=project_slug,
        display_name=display_name,
        primary_path=primary_path,
        board_slug=board_slug,
    )
