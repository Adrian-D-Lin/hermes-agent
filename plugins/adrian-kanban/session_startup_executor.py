"""Lean journaled project-onboarding operation executor (S2C2b).

This module is the S2C2b executor of the project-repository-binding work. It
runs the existing durable primitives in one fixed order against the
onboarding journal and returns the stable Project identity. It is deliberately
not a service, framework, database, or general rollback layer: it wires
primitives and journals their verified evidence.

Executor contract (as consumed by
:class:`session_startup_onboarding.OnboardingCoordinator`)::

    executor(record, proposal, persist) -> {"status": "created",
                                            "project": {"id": ...}}

The primitives run, in this exact order, on every invocation:

1. :meth:`RepositoryProvisioner.run_provisioning` — durable
   operation/GitHub/SSH/branch provisioning.
2. :func:`establish_canonical_checkout` with ``checkout_root=AI_MAIN_ROOT``.
3. :func:`persist_repository_registry`.
4. :func:`persist_project_binding`.
5. Exact created handback with the Project ID.

Every primitive is the real-state authority on retry. The executor never
skips a side effect merely because a journal step exists, and it never invents
a cross-store rollback: a crash after a side effect but before its journal
checkpoint is recovered by rerunning the primitive and then checkpointing the
verified evidence. Each primitive already revalidates/reuses exact state or
fails on conflict.

The initial ``record`` argument is a snapshot. After the provisioner
checkpoints, later executor checkpoints must parse ``onboarding_json`` from the
newest record returned by each ``persist(onboarding)`` call, not the stale
initial record. :class:`_OnboardingHolder` wraps the ``persist`` callback so it
retains and returns that newest record.

Operational/domain exceptions from the primitives reach
:class:`OnboardingCoordinator` unchanged, which converts them to
``blocked_recoverable``. No broad exception wrapping is added.

No credentials, tokens, environment values, command internals, or whole
config/DB rows are journaled: only the structured ``as_dict`` evidence the
primitives expose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict

from .session_startup_checkout import (
    CheckoutEvidence,
    establish_canonical_checkout,
)
from .session_startup_persistence import (
    ProjectPersistenceResult,
    RegistryPersistenceResult,
    persist_project_binding,
    persist_repository_registry,
)
from .session_startup_proposal import AI_MAIN_ROOT
from .session_startup_provisioning import RepositoryProvisioner

# Journal block reused from provisioning. The executor appends its three step
# names to the same ordered ``steps`` list and stores its evidence beneath the
# same ``step_evidence`` mapping the provisioner already uses.
_EXECUTOR_PROVISIONING_BLOCK = "provisioning"

# Executor step names, appended in order to the provisioning journal block.
STEP_CHECKOUT = "canonical_checkout_established"
STEP_REGISTRY = "repository_registry_persisted"
STEP_PROJECT_BINDING = "project_binding_persisted"

# Evidence keys beneath the provisioning block's ``step_evidence`` mapping.
EVIDENCE_CHECKOUT = "checkout"
EVIDENCE_REGISTRY = "registry"
EVIDENCE_PROJECT_BINDING = "project_binding"


@dataclass
class _OnboardingHolder:
    """Retains the newest onboarding record returned by each ``persist`` call.

    The ``record`` argument to the executor is a snapshot; every
    ``persist(onboarding)`` call CAS-writes the whole mapping and returns the
    newest record. Later executor checkpoints parse ``onboarding_json`` from
    that newest record, not from the stale initial snapshot.
    """

    current: Dict[str, Any]
    persist: Callable[[Dict[str, Any]], Dict[str, Any]]

    def persist_onboarding(self, onboarding: Dict[str, Any]) -> Dict[str, Any]:
        """Forward one onboarding mapping, store and return the newest record.

        The returned record is validated: it must be a mapping carrying a
        nonblank JSON-string ``onboarding_json`` that decodes to exactly the
        onboarding mapping just supplied. A persist that returns anything
        else — including a valid-but-different decoded object — is a broken
        CAS/persist contract and fails loudly rather than letting the
        executor fall back to stale state on the next checkpoint, including
        after the final Project checkpoint.
        """
        newest = self.persist(onboarding)
        if not isinstance(newest, dict):
            raise ValueError(
                "persist returned an invalid onboarding record: expected a "
                "mapping, got "
                f"{type(newest).__name__}"
            )
        onboarding_json = newest.get("onboarding_json")
        if not isinstance(onboarding_json, str) or not onboarding_json.strip():
            raise ValueError(
                "persist returned an invalid onboarding record: "
                "'onboarding_json' must be a nonblank JSON string"
            )
        try:
            decoded = json.loads(onboarding_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "persist returned an invalid onboarding record: "
                "'onboarding_json' is not valid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise ValueError(
                "persist returned an invalid onboarding record: "
                "'onboarding_json' must decode to a JSON object"
            )
        if decoded != onboarding:
            raise ValueError(
                "persist returned an invalid onboarding record: "
                "'onboarding_json' must decode to exactly the onboarding "
                "mapping just supplied"
            )
        self.current = newest
        return newest


def build_onboarding_operation_executor(
    provisioner: RepositoryProvisioner,
    *,
    establish_checkout: Callable[..., CheckoutEvidence] = establish_canonical_checkout,
    persist_registry: Callable[..., RegistryPersistenceResult] = persist_repository_registry,
    persist_project: Callable[..., ProjectPersistenceResult] = persist_project_binding,
) -> Callable[[Dict[str, Any], Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]], Dict[str, Any]]:
    """Build the journaled project-onboarding operation executor.

    ``provisioner`` is the real :class:`RepositoryProvisioner`. The checkout,
    registry, and Project persistence are injected as narrow callables so
    tests exercise temp state without a live seam; the production defaults
    are the real primitives. The factory does not copy any primitive
    implementation.

    The checkout root is the fixed canonical checkout root
    :data:`AI_MAIN_ROOT`. It is never derived from proposal text and is never
    a factory or test override: production and tests alike call checkout with
    the imported :data:`AI_MAIN_ROOT`, so a hostile ``canonical_checkout``
    parent in the proposal cannot redirect the checkout root.
    """

    def executor(
        record: Dict[str, Any],
        proposal: Dict[str, Any],
        persist: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        holder = _OnboardingHolder(current=record, persist=persist)

        # Step 1: durable operation/GitHub/SSH/branch provisioning. The
        # provisioner checkpoints its own steps through the holder's persist.
        provision_evidence = provisioner.run_provisioning(
            holder.current, holder.persist_onboarding
        )

        # Step 2: establish (or revalidate) the canonical checkout. The
        # checkout is the real-state authority: rerun on every invocation.
        # The checkout root is always the fixed imported AI_MAIN_ROOT, never
        # derived from the proposal's canonical_checkout parent.
        checkout_evidence = establish_checkout(
            checkout=proposal["canonical_checkout"],
            repository=proposal["repository"],
            integration_branch=proposal["integration_branch"],
            base_sha=provision_evidence["branch_sha"],
            checkout_root=AI_MAIN_ROOT,
        )
        holder.persist_onboarding(_checkpoint_out(
            holder.current,
            STEP_CHECKOUT,
            {EVIDENCE_CHECKOUT: checkout_evidence.as_dict()},
        ))

        # Step 3: persist the trusted repository registry.
        registry_result = persist_registry(
            proposal,
            checkout_evidence,
        )
        holder.persist_onboarding(_checkpoint_out(
            holder.current,
            STEP_REGISTRY,
            {EVIDENCE_REGISTRY: registry_result.as_dict()},
        ))

        # Step 4: create/confirm the Hermes Project binding.
        project_result = persist_project(
            proposal,
        )
        holder.persist_onboarding(_checkpoint_out(
            holder.current,
            STEP_PROJECT_BINDING,
            {EVIDENCE_PROJECT_BINDING: project_result.as_dict()},
        ))

        # Step 5: exact created handback with the stable Project ID.
        return {
            "status": "created",
            "project": {"id": project_result.project_id},
        }

    return executor


def _checkpoint_out(
    current: Dict[str, Any],
    step: str,
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a copy of the newest onboarding with ``step`` journaled.

    Parses the onboarding object from ``current["onboarding_json"]`` so later
    checkpoints never observe the stale initial record, appends ``step`` to
    the existing ordered ``steps`` list under the provisioning block, and
    stores ``evidence`` beneath its ``step_evidence`` mapping, preserving
    prior steps and prior evidence.

    The stored onboarding is expected to be well-formed: a JSON object with a
    ``journal`` mapping and a provisioning block that is a mapping with
    ``steps``/``step_evidence``. This is invariant because
    :func:`_checkpoint_out` runs only after a successful
    :class:`RepositoryProvisioner` completion, whose checkpoints must have
    created ``journal["provisioning"]``. A malformed or missing structure
    means the stored onboarding is corrupted; this fails clearly instead of
    silently substituting an empty object.

    Only the dictionaries and lists that are modified are copied, so a failed
    persist does not corrupt the prior holder record in memory: the returned
    mapping is a fresh parse of the newest record, and prior nested
    dictionaries/lists that are preserved are carried over by reference.
    """
    onboarding_json = current.get("onboarding_json")
    if not isinstance(onboarding_json, str) or not onboarding_json.strip():
        raise ValueError(
            "stored onboarding is corrupted: 'onboarding_json' is missing or "
            "blank in the newest record"
        )
    try:
        onboarding = json.loads(onboarding_json)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "stored onboarding is corrupted: 'onboarding_json' is not valid "
            "JSON"
        ) from exc
    if not isinstance(onboarding, dict):
        raise ValueError(
            "stored onboarding is corrupted: 'onboarding_json' must decode "
            "to a JSON object"
        )

    journal = onboarding.get("journal")
    if not isinstance(journal, dict):
        raise ValueError(
            "stored onboarding is corrupted: 'journal' is missing or not a "
            "mapping"
        )

    block = journal.get(_EXECUTOR_PROVISIONING_BLOCK)
    if not isinstance(block, dict):
        raise ValueError(
            "stored onboarding is corrupted: the provisioning journal block "
            "is missing or not a mapping"
        )

    existing_steps = block.get("steps")
    if not isinstance(existing_steps, list):
        raise ValueError(
            "stored onboarding is corrupted: provisioning 'steps' is missing "
            "or not a list"
        )
    steps = list(existing_steps)
    if step not in steps:
        steps.append(step)
    block["steps"] = steps

    step_evidence = block.get("step_evidence")
    if not isinstance(step_evidence, dict):
        raise ValueError(
            "stored onboarding is corrupted: provisioning 'step_evidence' "
            "is missing or not a mapping"
        )
    step_evidence.update(evidence)
    block["step_evidence"] = step_evidence

    journal[_EXECUTOR_PROVISIONING_BLOCK] = block
    onboarding["journal"] = journal
    return onboarding
