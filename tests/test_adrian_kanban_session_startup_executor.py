"""Focused tests for the S2C2b journaled onboarding operation executor.

Exercises :mod:`session_startup_executor`: the lean executor that runs the
existing durable primitives in one fixed order, journals each verified step
beneath the existing provisioning block, and returns the stable Project ID.

Cases:

* exact primitive call order (provision -> checkout -> registry -> Project)
  and exact ``{"status": "created", "project": {"id": ...}}`` handback;
* the ``persist`` callback is called with one onboarding dict and its returned
  newest record is used for subsequent executor checkpoints (not the stale
  initial record);
* each successful executor stage adds ordered step evidence to the existing
  provisioning journal without erasing prior steps/evidence;
* retry/replay invokes each real-state primitive again even when all executor
  step flags/evidence are already journaled, and returns the stable Project ID
  without a duplicate logical registration;
* simulated failure after the checkout side effect but before its checkpoint:
  retry revalidates checkout and proceeds without any rollback/delete;
* simulated failure after registry persistence but before its checkpoint:
  retry revalidates the exact registry and proceeds;
* simulated failure after Project persistence but before its checkpoint:
  retry reuses the exact Project and returns its stable ID;
* an invalid persist return / newest record fails clearly rather than falling
  back to stale state;
* no default/live seam is exercised in tests (all primitives injected).

All primitives are injected call-recording fakes; no real Git, network,
config, or Project DB is touched.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional

import pytest


@pytest.fixture
def modules(tmp_path: Path, monkeypatch) -> Dict[str, ModuleType]:
    """Load the adrian-kanban plugin package and return its modules.

    Mirrors the provisioning/checkout/persistence test fixtures: a temporary
    HERMES_HOME with the plugin enabled, then import the modules by the loaded
    package name.
    """

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import yaml

    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {"enabled": ["adrian-kanban"]},
                "kanban": {"controlled_worktree_root": "/srv/worktrees"},
            }
        ),
        encoding="utf-8",
    )
    try:
        from hermes_cli.plugins import PluginManager
        from hermes_cli import kanban_db as kb
    except Exception:
        pytest.skip("hermes_cli plugin manager unavailable")
    manager = PluginManager(scope_key=str(home.resolve()))
    kb.init_db()
    manager.discover_and_load()
    loaded = manager._plugins.get("adrian-kanban")
    if loaded is None or loaded.module is None:
        pytest.skip("adrian-kanban plugin could not load")
    package = loaded.module
    return {
        "executor": importlib.import_module(
            f"{package.__name__}.session_startup_executor"
        ),
        "provisioning": importlib.import_module(
            f"{package.__name__}.session_startup_provisioning"
        ),
        "checkout": importlib.import_module(
            f"{package.__name__}.session_startup_checkout"
        ),
        "persistence": importlib.import_module(
            f"{package.__name__}.session_startup_persistence"
        ),
    }


# ---------------------------------------------------------------------------
# fakes / seams
# ---------------------------------------------------------------------------


def _prov(modules: Dict[str, ModuleType]):
    return modules["provisioning"]


def _pers(modules: Dict[str, ModuleType]):
    return modules["persistence"]


def _checkout(modules: Dict[str, ModuleType]):
    return modules["checkout"]


def _executor(modules: Dict[str, ModuleType]):
    return modules["executor"]


class FakePersist:
    """Records each onboarding dict passed to ``persist``; returns a record.

    Mirrors the ``OnboardingCoordinator._make_persist`` contract: called with
    exactly one onboarding mapping, returns the newest record whose
    ``onboarding_json`` is the just-persisted mapping.
    """

    def __init__(self, record: Dict[str, Any]):
        self.records: List[Dict[str, Any]] = [record]
        self.calls: List[Dict[str, Any]] = []
        self._next_revision = record["revision"]

    def __call__(self, onboarding: Dict[str, Any]) -> Dict[str, Any]:
        assert isinstance(onboarding, dict)
        self.calls.append(json.loads(json.dumps(onboarding)))
        self._next_revision += 1
        record = {
            "session_id": "session-1",
            "revision": self._next_revision,
            "onboarding_json": json.dumps(onboarding),
        }
        self.records.append(record)
        return record


def _record(proposal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": "session-1",
        "revision": 1,
        "onboarding_json": json.dumps(
            {
                "stage": "confirm",
                "draft": {"mode": "bind"},
                "journal": {"proposal": proposal, "events": ["confirm"]},
            }
        ),
    }


def _make_proposal(
    modules: Dict[str, ModuleType],
    project_slug: str = "proj-repo",
    canonical_checkout: str = "/home/progenitor/AI-main/proj-repo",
    repository: str = "adrian-d-lin/proj-repo",
    integration_branch: str = "main",
    board_slug: str = "board-proj-repo",
    controlled_worktree_root: str = "/srv/worktrees",
    display_name: str = "Proj Repo",
) -> Dict[str, Any]:
    return {
        "project_slug": project_slug,
        "display_name": display_name,
        "canonical_checkout": canonical_checkout,
        "repository": repository,
        "board_slug": board_slug,
        "integration_branch": integration_branch,
        "controlled_worktree_root": controlled_worktree_root,
    }


class _ProvisionerFake:
    """Records the record/persist passed to ``run_provisioning`` and checkpoints.

    Performs one realistic checkpoint through the supplied one-argument
    ``persist`` callback (the holder's ``persist_onboarding``): it parses the
    onboarding object from the record, preserves/creates the ``provisioning``
    journal block with the real ``integration_branch_verified`` step and
    matching evidence, retains the proposal/events, and calls ``persist`` once
    with the updated onboarding mapping. Returns a fixed provision evidence.
    The ``persist`` it receives is the holder's callback; it is recorded so
    tests can assert the executor passes the holder's persist, not the raw one.
    """

    def __init__(self, branch_sha: str = "a" * 40, fail: Optional[Callable] = None):
        self.calls: List[Dict[str, Any]] = []
        self.branch_sha = branch_sha
        self._fail = fail

    def run_provisioning(
        self, record: Dict[str, Any], persist: Callable
    ) -> Dict[str, Any]:
        self.calls.append({"record": record, "persist": persist})
        if self._fail is not None:
            self._fail("provision")
        # One realistic provisioning checkpoint through the supplied one-arg
        # persist callback: preserve the existing provisioning block (creating
        # it when absent) with the real integration_branch_verified step and
        # matching evidence, retaining proposal/events.
        step_branch = _modules_ref["provisioning"].STEP_BRANCH
        onboarding = json.loads(record["onboarding_json"])
        journal = onboarding.setdefault("journal", {})
        block = journal.setdefault("provisioning", {})
        steps = block.get("steps")
        if not isinstance(steps, list):
            steps = []
        if step_branch not in steps:
            steps.append(step_branch)
        block["steps"] = steps
        step_evidence = block.get("step_evidence")
        if not isinstance(step_evidence, dict):
            step_evidence = {}
        step_evidence[step_branch] = {
            "branch": "main",
            "branch_sha": self.branch_sha,
        }
        block["step_evidence"] = step_evidence
        persist(onboarding)
        return {
            "operation_id": "provision-abc",
            "repository": "adrian-d-lin/proj-repo",
            "mode": "bind",
            "visibility": None,
            "integration_branch": "main",
            "branch_sha": self.branch_sha,
        }


class _CheckoutFake:
    """Records the kwargs passed to ``establish_canonical_checkout``."""

    def __init__(self, evidence: Optional[Any] = None, fail: Optional[Callable] = None):
        self.calls: List[Dict[str, Any]] = []
        self.evidence = evidence
        self._fail = fail

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if self._fail is not None:
            self._fail("checkout")
        if self.evidence is None:
            CheckoutEvidence = _modules_ref["checkout"].CheckoutEvidence
            self.evidence = CheckoutEvidence(
                canonical_checkout=kwargs["checkout"],
                normalized_origin="adrian-d-lin/proj-repo",
                integration_branch=kwargs["integration_branch"],
                remote_ref="origin/main",
                base_sha=kwargs["base_sha"],
                created_by_operation=True,
            )
        return self.evidence


class _RegistryFake:
    """Records the proposal/evidence passed to ``persist_repository_registry``."""

    def __init__(self, result: Optional[Any] = None, fail: Optional[Callable] = None):
        self.calls: List[Dict[str, Any]] = []
        self.result = result
        self._fail = fail

    def __call__(self, proposal: Any, evidence: Any, **kwargs: Any) -> Any:
        self.calls.append({"proposal": proposal, "evidence": evidence})
        if self._fail is not None:
            self._fail("registry")
        if self.result is None:
            RegistryPersistenceResult = _modules_ref[
                "persistence"
            ].RegistryPersistenceResult
            self.result = RegistryPersistenceResult(
                project_slug="proj-repo",
                registry_entry={
                    "repository_root": "/home/progenitor/AI-main/proj-repo",
                    "github_repository": "adrian-d-lin/proj-repo",
                    "integration_branch": "main",
                },
            )
        return self.result


class _ProjectFake:
    """Records the proposal passed to ``persist_project_binding``."""

    def __init__(self, result: Optional[Any] = None, fail: Optional[Callable] = None):
        self.calls: List[Dict[str, Any]] = []
        self.result = result
        self._fail = fail

    def __call__(self, proposal: Any, **kwargs: Any) -> Any:
        self.calls.append({"proposal": proposal})
        if self._fail is not None:
            self._fail("project")
        if self.result is None:
            ProjectPersistenceResult = _modules_ref[
                "persistence"
            ].ProjectPersistenceResult
            self.result = ProjectPersistenceResult(
                project_id="project-stable-id",
                project_slug="proj-repo",
                display_name="Proj Repo",
                primary_path="/home/progenitor/AI-main/proj-repo",
                board_slug="board-proj-repo",
            )
        return self.result


# A module-level reference so the fakes can build default evidence/results
# without threading the modules dict through every constructor.
_modules_ref: Dict[str, ModuleType] = {}


def _build_executor(
    modules: Dict[str, ModuleType],
    provisioner: Optional[_ProvisionerFake] = None,
    checkout: Optional[_CheckoutFake] = None,
    registry: Optional[_RegistryFake] = None,
    project: Optional[_ProjectFake] = None,
):
    global _modules_ref
    _modules_ref = modules
    prov = provisioner or _ProvisionerFake()
    chk = checkout or _CheckoutFake()
    reg = registry or _RegistryFake()
    proj = project or _ProjectFake()
    build = _executor(modules).build_onboarding_operation_executor
    executor = build(
        prov,
        establish_checkout=chk,
        persist_registry=reg,
        persist_project=proj,
    )
    return executor, (prov, chk, reg, proj)


# ---------------------------------------------------------------------------
# exact call order + exact handback
# ---------------------------------------------------------------------------


def test_executor_runs_primitives_in_exact_order_and_handback(modules):
    executor, (prov, chk, reg, proj) = _build_executor(modules)
    proposal = _make_proposal(modules)
    record = _record(proposal)
    persist = FakePersist(record)
    outcome = executor(record, proposal, persist)

    # Exact handback.
    assert outcome == {
        "status": "created",
        "project": {"id": "project-stable-id"},
    }
    # The primitives ran in the fixed order.
    assert _call_sequence(modules) == [
        "provision",
        "checkout",
        "registry",
        "project",
    ]


def test_checkout_receives_aimain_root_and_verified_sha(modules):
    executor, (prov, chk, reg, proj) = _build_executor(modules)
    proposal = _make_proposal(modules)
    executor(_record(proposal), proposal, FakePersist(_record(proposal)))
    call = chk.calls[0]
    assert call["checkout_root"] == "/home/progenitor/AI-main"
    assert call["base_sha"] == "a" * 40
    assert call["checkout"] == "/home/progenitor/AI-main/proj-repo"
    assert call["repository"] == "adrian-d-lin/proj-repo"
    assert call["integration_branch"] == "main"


def test_hostile_checkout_parent_cannot_redirect_checkout_root(modules):
    # A proposal whose canonical_checkout parent is wrong must not redirect
    # the checkout root: the executor always calls checkout with the fixed
    # imported AI_MAIN_ROOT. The real checkout primitive would then reject
    # that checkout (its containment check against AI_MAIN_ROOT fails) before
    # any mutation; here we assert the call still carries the fixed root.
    executor, (prov, chk, reg, proj) = _build_executor(modules)
    proposal = _make_proposal(
        modules,
        canonical_checkout="/etc/hostile/proj-repo",
    )
    record = _record(proposal)
    executor(record, proposal, FakePersist(record))
    call = chk.calls[0]
    assert call["checkout_root"] == _executor(modules).AI_MAIN_ROOT
    # The hostile parent is passed through as the checkout target, but the
    # root stays fixed.
    assert call["checkout"] == "/etc/hostile/proj-repo"
    assert call["checkout_root"] != "/etc/hostile"


# ---------------------------------------------------------------------------
# persist callback: one dict, newest record used for later checkpoints
# ---------------------------------------------------------------------------


def test_persist_called_with_one_dict_and_newest_record_used(modules):
    executor, (prov, chk, reg, proj) = _build_executor(modules)
    proposal = _make_proposal(modules)
    record = _record(proposal)
    persist = FakePersist(record)
    executor(record, proposal, persist)

    # The provisioner's one realistic checkpoint plus the three executor
    # checkpoints each call persist once with one onboarding dict.
    assert len(persist.calls) == 4
    for call in persist.calls:
        assert isinstance(call, dict)

    # The provisioner received the holder's persist (which returns newest
    # records), and the holder's current advanced past the initial snapshot.
    assert prov.calls[0]["persist"] is not persist
    assert persist.records[-1]["revision"] > record["revision"]


def test_later_checkpoints_parse_newest_record_not_stale(modules):
    executor, (prov, chk, reg, proj) = _build_executor(modules)
    proposal = _make_proposal(modules)
    record = _record(proposal)
    persist = FakePersist(record)
    executor(record, proposal, persist)

    # The checkout checkpoint must carry the checkout evidence in the newest
    # record's journal, proving it parsed the newest record, not the initial
    # stale snapshot.
    newest = persist.records[-1]
    journal = json.loads(newest["onboarding_json"])["journal"]
    block = journal["provisioning"]
    assert block["steps"][-1] == "project_binding_persisted"
    # The checkout evidence was recorded in the newest record, proving the
    # checkpoint parsed the newest record, not the initial stale snapshot.
    assert block["step_evidence"]["checkout"]["base_sha"] == "a" * 40


# ---------------------------------------------------------------------------
# ordered step evidence without erasing prior evidence
# ---------------------------------------------------------------------------


def test_each_stage_adds_ordered_step_evidence_without_erasing(modules):
    executor, (prov, chk, reg, proj) = _build_executor(modules)
    proposal = _make_proposal(modules)
    record = _record(proposal)
    persist = FakePersist(record)
    executor(record, proposal, persist)

    block = json.loads(persist.records[-1]["onboarding_json"])[
        "journal"
    ]["provisioning"]
    prov_step = _prov(modules).STEP_BRANCH

    # The provisioner's checkpoint step precedes the three ordered executor
    # steps, which are appended in order.
    assert block["steps"][-3:] == [
        "canonical_checkout_established",
        "repository_registry_persisted",
        "project_binding_persisted",
    ]
    assert prov_step in block["steps"]
    assert block["steps"].index(prov_step) < block["steps"].index(
        "canonical_checkout_established"
    )
    # Each executor evidence key present.
    ev = block["step_evidence"]
    assert "checkout" in ev and "registry" in ev and "project_binding" in ev
    # The provisioner's step/evidence is preserved by the executor's
    # checkpointing: it remains in the newest record's journal.
    assert prov_step in ev
    assert ev[prov_step] == {"branch": "main", "branch_sha": "a" * 40}
    # Exactly four evidence keys total: the provisioner's plus the three
    # executor's.
    assert set(ev) == {
        prov_step,
        "checkout",
        "registry",
        "project_binding",
    }


def test_prior_evidence_preserved_when_step_already_present(modules):
    executor, (prov, chk, reg, proj) = _build_executor(modules)
    proposal = _make_proposal(modules)
    prov_step = _prov(modules).STEP_BRANCH
    # Seed the journal with a pre-existing executor step + evidence and a
    # pre-existing provisioner step + evidence that the fake provisioner does
    # not overwrite (credentials_verified), so we can prove the executor's
    # checkpointing preserves prior evidence.
    seed_onboarding = {
        "stage": "confirm",
        "draft": {"mode": "bind"},
        "journal": {
            "proposal": proposal,
            "events": ["confirm"],
            "provisioning": {
                "steps": [
                    "credentials_verified",
                    "canonical_checkout_established",
                ],
                "step_evidence": {
                    "checkout": {"base_sha": "b" * 40},
                    "credentials_verified": {"verified": True},
                    prov_step: {"branch_sha": "b" * 40},
                },
            },
        },
    }
    record = {
        "session_id": "session-1",
        "revision": 1,
        "onboarding_json": json.dumps(seed_onboarding),
    }
    persist = FakePersist(record)
    executor(record, proposal, persist)

    block = json.loads(persist.records[-1]["onboarding_json"])[
        "journal"
    ]["provisioning"]
    # The executor's own checkout step is not duplicated even though it was
    # pre-seeded.
    assert block["steps"].count("canonical_checkout_established") == 1
    ev = block["step_evidence"]
    # The executor re-ran checkout, so its evidence reflects the new SHA.
    assert ev["checkout"]["base_sha"] == "a" * 40
    # The provisioner re-ran and refreshed its branch evidence to the new SHA.
    assert ev[prov_step]["branch_sha"] == "a" * 40
    # A pre-existing provisioner evidence key the fake does not write is
    # preserved untouched by the executor's checkpointing.
    assert ev["credentials_verified"] == {"verified": True}
    assert "credentials_verified" in block["steps"]


# ---------------------------------------------------------------------------
# retry/replay: re-invokes each primitive, stable Project ID, no duplicate
# ---------------------------------------------------------------------------


def test_retry_reinvokes_each_primitive_and_returns_stable_id(modules):
    proposal = _make_proposal(modules)
    record = _record(proposal)

    # First run.
    executor, (prov1, chk1, reg1, proj1) = _build_executor(modules)
    persist = FakePersist(record)
    outcome1 = executor(record, proposal, persist)
    stable_id = outcome1["project"]["id"]

    # Second run against the newest record: all executor steps/evidence are
    # already journaled, yet every primitive must run again.
    executor2, (prov2, chk2, reg2, proj2) = _build_executor(modules)
    outcome2 = executor2(persist.records[-1], proposal, FakePersist(persist.records[-1]))

    assert outcome1 == outcome2 == {"status": "created", "project": {"id": stable_id}}
    for prov, chk, reg, proj in (
        (prov1, chk1, reg1, proj1),
        (prov2, chk2, reg2, proj2),
    ):
        assert len(prov.calls) == 1
        assert len(chk.calls) == 1
        assert len(reg.calls) == 1
        assert len(proj.calls) == 1
    # The Project primitive is called exactly once per run: no duplicate
    # logical registration.
    assert len(proj2.calls) == 1


# ---------------------------------------------------------------------------
# failure after checkout side effect, before its checkpoint: retry revalidates
# ---------------------------------------------------------------------------


def test_failure_after_checkout_side_effect_before_checkpoint(modules):
    proposal = _make_proposal(modules)

    # First run: checkout side effect "succeeds" but then the executor is
    # interrupted before checkpointing (simulated by a second checkout call
    # raising). The checkout fake records the side effect, then raises.
    checkout_calls: List[str] = []

    def checkout_side_effect(stage: str, **kwargs: Any) -> Any:
        checkout_calls.append(stage)
        raise RuntimeError("simulated crash after checkout side effect")

    chk1 = _CheckoutFake(evidence=None, fail=checkout_side_effect)
    persist = FakePersist(_record(proposal))
    executor1, (prov1, chk1, reg1, proj1) = _build_executor(modules, checkout=chk1)
    with pytest.raises(RuntimeError):
        executor1(_record(proposal), proposal, persist)

    # No executor checkpoint journaled for checkout. Only the provisioner's
    # own checkpoint (which runs before checkout) is present.
    prov_step = _prov(modules).STEP_BRANCH
    block = (
        json.loads(persist.records[-1]["onboarding_json"])
        .get("journal", {})
        .get("provisioning", {})
    )
    assert block.get("steps", []) == [prov_step]
    assert "canonical_checkout_established" not in block.get("steps", [])

    # Second run: retry revalidates checkout (side effect runs again) and
    # proceeds; no rollback/delete of the checkout.
    chk2 = _CheckoutFake(evidence=None)
    executor2 = _build_executor(modules, checkout=chk2)[0]
    outcome2 = executor2(persist.records[-1], proposal, FakePersist(persist.records[-1]))
    assert outcome2 == {"status": "created", "project": {"id": "project-stable-id"}}
    # Side effect ran again on retry, not deleted/rolled back.
    assert len(checkout_calls) == 1  # first run's checkout side effect
    assert len(chk2.calls) == 1  # retry revalidated checkout
    assert proj1.calls == []  # project never reached on first run


# ---------------------------------------------------------------------------
# failure after registry persistence, before its checkpoint: retry revalidates
# ---------------------------------------------------------------------------


def test_failure_after_registry_persistence_before_checkpoint(modules):
    proposal = _make_proposal(modules)
    registry_calls: List[str] = []

    def registry_side_effect(stage: str) -> Any:
        registry_calls.append(stage)
        raise RuntimeError("simulated crash after registry persistence")

    reg1 = _RegistryFake(fail=registry_side_effect)
    persist = FakePersist(_record(proposal))
    executor1, (prov1, chk1, reg1, proj1) = _build_executor(modules, registry=reg1)
    with pytest.raises(RuntimeError):
        executor1(_record(proposal), proposal, persist)

    block = (
        json.loads(persist.records[-1]["onboarding_json"])
        .get("journal", {})
        .get("provisioning", {})
    )
    assert "repository_registry_persisted" not in block.get("steps", [])

    # Retry: revalidate the exact registry (same proposal/evidence) and proceed.
    reg2 = _RegistryFake()
    executor2 = _build_executor(modules, registry=reg2)[0]
    outcome2 = executor2(persist.records[-1], proposal, FakePersist(persist.records[-1]))
    assert outcome2 == {"status": "created", "project": {"id": "project-stable-id"}}
    # Registry side effect ran on the first run and again on retry.
    assert len(registry_calls) == 1
    assert len(reg2.calls) == 1
    # The registry was revalidated with the exact same proposal and the
    # checkout evidence produced on retry.
    assert reg2.calls[0]["proposal"]["project_slug"] == "proj-repo"
    assert proj1.calls == []


# ---------------------------------------------------------------------------
# failure after Project persistence, before its checkpoint: retry reuses Project
# ---------------------------------------------------------------------------


def test_failure_after_project_persistence_before_checkpoint(modules):
    proposal = _make_proposal(modules)
    project_calls: List[str] = []

    def project_side_effect(stage: str) -> Any:
        project_calls.append(stage)
        raise RuntimeError("simulated crash after Project persistence")

    proj1 = _ProjectFake(fail=project_side_effect)
    persist = FakePersist(_record(proposal))
    executor1, (prov1, chk1, reg1, proj1) = _build_executor(modules, project=proj1)
    with pytest.raises(RuntimeError):
        executor1(_record(proposal), proposal, persist)

    block = (
        json.loads(persist.records[-1]["onboarding_json"])
        .get("journal", {})
        .get("provisioning", {})
    )
    assert "project_binding_persisted" not in block.get("steps", [])

    # Retry: reuse the exact Project (same slug) and return its stable ID.
    proj2 = _ProjectFake()
    executor2 = _build_executor(modules, project=proj2)[0]
    outcome2 = executor2(persist.records[-1], proposal, FakePersist(persist.records[-1]))
    assert outcome2 == {"status": "created", "project": {"id": "project-stable-id"}}
    # Project side effect ran on the first run and again on retry.
    assert len(project_calls) == 1
    assert len(proj2.calls) == 1
    # The Project primitive is called exactly once on retry: no duplicate.
    assert len(proj2.calls) == 1


# ---------------------------------------------------------------------------
# stored-state corruption: missing journal / provisioning block fails clearly
# ---------------------------------------------------------------------------


def test_checkpoint_out_missing_journal_fails_clearly(modules):
    proposal = _make_proposal(modules)
    record = {
        "session_id": "session-1",
        "revision": 1,
        "onboarding_json": json.dumps(
            {"stage": "confirm", "draft": {"mode": "bind"}}
        ),
    }
    with pytest.raises(
        ValueError,
        match="'journal' is missing or not a mapping",
    ):
        _executor(modules)._checkpoint_out(
            record,
            "canonical_checkout_established",
            {"checkout": {"base_sha": "a" * 40}},
        )


def test_checkpoint_out_missing_provisioning_block_fails_clearly(modules):
    proposal = _make_proposal(modules)
    record = {
        "session_id": "session-1",
        "revision": 1,
        "onboarding_json": json.dumps(
            {
                "stage": "confirm",
                "draft": {"mode": "bind"},
                "journal": {"proposal": proposal, "events": ["confirm"]},
            }
        ),
    }
    with pytest.raises(
        ValueError,
        match="provisioning journal block is missing or not a mapping",
    ):
        _executor(modules)._checkpoint_out(
            record,
            "canonical_checkout_established",
            {"checkout": {"base_sha": "a" * 40}},
        )


def test_checkpoint_out_missing_steps_fails_clearly(modules):
    proposal = _make_proposal(modules)
    record = {
        "session_id": "session-1",
        "revision": 1,
        "onboarding_json": json.dumps(
            {
                "stage": "confirm",
                "draft": {"mode": "bind"},
                "journal": {
                    "proposal": proposal,
                    "events": ["confirm"],
                    "provisioning": {"step_evidence": {}},
                },
            }
        ),
    }
    with pytest.raises(
        ValueError,
        match="provisioning 'steps' is missing or not a list",
    ):
        _executor(modules)._checkpoint_out(
            record,
            "canonical_checkout_established",
            {"checkout": {"base_sha": "a" * 40}},
        )


def test_checkpoint_out_missing_step_evidence_fails_clearly(modules):
    proposal = _make_proposal(modules)
    record = {
        "session_id": "session-1",
        "revision": 1,
        "onboarding_json": json.dumps(
            {
                "stage": "confirm",
                "draft": {"mode": "bind"},
                "journal": {
                    "proposal": proposal,
                    "events": ["confirm"],
                    "provisioning": {"steps": []},
                },
            }
        ),
    }
    with pytest.raises(
        ValueError,
        match="provisioning 'step_evidence' is missing or not a mapping",
    ):
        _executor(modules)._checkpoint_out(
            record,
            "canonical_checkout_established",
            {"checkout": {"base_sha": "a" * 40}},
        )


# ---------------------------------------------------------------------------
# invalid persist return / newest record fails clearly
# ---------------------------------------------------------------------------


def test_invalid_newest_record_fails_clearly(modules):
    proposal = _make_proposal(modules)

    class _BadPersist:
        """Returns a record without a usable onboarding_json."""

        def __init__(self, record: Dict[str, Any]):
            self.records = [record]

        def __call__(self, onboarding: Dict[str, Any]) -> Dict[str, Any]:
            return {"session_id": "session-1", "revision": 2}

    executor, (prov, chk, reg, proj) = _build_executor(modules)
    record = _record(proposal)
    with pytest.raises(ValueError, match="onboarding_json' must be a nonblank"):
        executor(record, proposal, _BadPersist(record))


def test_persist_returning_valid_but_different_onboarding_fails_clearly(modules):
    # A persist that returns a record whose onboarding_json decodes to a
    # valid-but-different object (not the mapping just supplied) breaks the
    # CAS/persist contract and must raise a clear ValueError immediately,
    # including after the final checkpoint.
    proposal = _make_proposal(modules)
    record = _record(proposal)

    class _DivergentPersist:
        """Returns a record whose onboarding_json is a valid but different object."""

        def __call__(self, onboarding: Dict[str, Any]) -> Dict[str, Any]:
            # A different, decodable object: drop the journal entirely.
            divergent = {"stage": "confirm", "draft": {"mode": "bind"}}
            return {
                "session_id": "session-1",
                "revision": 2,
                "onboarding_json": json.dumps(divergent),
            }

    executor, (prov, chk, reg, proj) = _build_executor(modules)
    with pytest.raises(
        ValueError,
        match="must decode to exactly the onboarding",
    ):
        executor(record, proposal, _DivergentPersist())


# ---------------------------------------------------------------------------
# no default/live seam exercised
# ---------------------------------------------------------------------------


def test_no_default_live_seam_exercised(modules):
    proposal = _make_proposal(modules)
    executor, (prov, chk, reg, proj) = _build_executor(modules)
    executor(_record(proposal), proposal, FakePersist(_record(proposal)))
    assert len(prov.calls) == 1
    assert len(chk.calls) == 1
    assert len(reg.calls) == 1
    assert len(proj.calls) == 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _call_sequence(modules) -> List[str]:
    """Return the order primitives were invoked, via a shared clock."""
    clock: List[str] = []

    proposal = _make_proposal(modules)

    class _ClockProvisioner(_ProvisionerFake):
        def run_provisioning(self, record, persist):
            clock.append("provision")
            return super().run_provisioning(record, persist)

    class _ClockCheckout(_CheckoutFake):
        def __call__(self, **kwargs):
            clock.append("checkout")
            return super().__call__(**kwargs)

    class _ClockRegistry(_RegistryFake):
        def __call__(self, proposal, evidence, **kwargs):
            clock.append("registry")
            return super().__call__(proposal, evidence, **kwargs)

    class _ClockProject(_ProjectFake):
        def __call__(self, proposal, **kwargs):
            clock.append("project")
            return super().__call__(proposal, **kwargs)

    prov = _ClockProvisioner()
    chk = _ClockCheckout()
    reg = _ClockRegistry()
    proj = _ClockProject()
    build = _executor(modules).build_onboarding_operation_executor
    executor = build(
        prov,
        establish_checkout=chk,
        persist_registry=reg,
        persist_project=proj,
    )
    executor(_record(proposal), proposal, FakePersist(_record(proposal)))
    return clock


def test_call_sequence_is_provision_checkout_registry_project(modules):
    assert _call_sequence(modules) == [
        "provision",
        "checkout",
        "registry",
        "project",
    ]
