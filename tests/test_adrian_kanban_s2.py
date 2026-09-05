"""Independent S2 tests derived from the ratified v0.28 design oracle."""

from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import pickle
import sqlite3
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from gateway import trusted_authorizer_evidence as trusted
from writegate.kanban_approvals import (
    APPROVAL_TYPE,
    KanbanApprovalRejected,
    KanbanInitiativeApprovalConsumption,
    KanbanInitiativeApprovalHost,
    KanbanInitiativeApprovalPreparation,
    consume_approved,
    create_kanban_approval_schema,
)


@pytest.fixture(scope="module")
def capability_module():
    path = (
        Path(__file__).parents[1]
        / "plugins"
        / "adrian-kanban"
        / "capability.py"
    )
    spec = importlib.util.spec_from_file_location("s2_capability", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture(scope="module")
def provider_modules():
    root = Path(__file__).parents[1] / "plugins" / "adrian-kanban"
    name = "s2_adrian_kanban"
    spec = importlib.util.spec_from_file_location(
        name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    modules = {
        "capability": importlib.import_module(f"{name}.capability"),
        "diagnostics": importlib.import_module(f"{name}.diagnostics"),
        "provider": importlib.import_module(f"{name}.provider"),
    }
    yield modules
    for module_name in tuple(sys.modules):
        if module_name == name or module_name.startswith(f"{name}."):
            sys.modules.pop(module_name, None)


def _binding(module, **changes):
    values = {
        "operation": "native_create_task",
        "target": "task-1",
        "expected_version": 3,
        "canonical_digest": "sha256:task-payload",
        "session_id": "session-1",
        "workspace_id": "workspace-1",
        "plugin_version": "0.2.0",
        "protocol_version": "2",
        "execution_context": "run-1",
    }
    values.update(changes)
    return module.CapabilityBinding(**values)


def _select_plugin_authority(tmp_path, monkeypatch, database_path: Path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "kanban:\n"
        "  mutation_authority: adrian-kanban\n"
        f"  database_path: {database_path.as_posix()}\n",
        encoding="utf-8",
    )


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    create_kanban_approval_schema(conn)
    return conn


def _trusted_evidence(*, request_id: str = "request-1"):
    connection = trusted._record_authenticated_tailscale_connection(
        "gateway/tailscale",
        "connection-1",
        "adrian@tailnet",
        request_id,
        1_000,
    )
    return trusted.TrustedAuthorizerEvidence._from_authenticated_connection(
        connection,
        issued_at=1_001,
        ttl_seconds=120,
    )


def _preparation(**changes) -> KanbanInitiativeApprovalPreparation:
    values = {
        "approval_id": "approval-1",
        "request_id": "request-1",
        "operation": "kanban_update_initiative",
        "initiative_id": "initiative-1",
        "proposed_creation_id": None,
        "expected_version": 4,
        "canonical_digest": "sha256:payload",
        "session_id": "session-1",
        "expires_at": 1_200,
    }
    values.update(changes)
    return KanbanInitiativeApprovalPreparation(**values)


def _consumption(authorizer_evidence: str, **changes):
    values = {
        "approval_id": "approval-1",
        "request_id": "request-1",
        "operation": "kanban_update_initiative",
        "initiative_id": "initiative-1",
        "proposed_creation_id": None,
        "expected_version": 4,
        "canonical_digest": "sha256:payload",
        "canonicalization_version": 1,
        "authorizer_evidence": authorizer_evidence,
        "session_id": "session-1",
        "expires_at": 1_200,
        "consumed_mutation_id": "mutation-1",
        "consumed_idempotency_ref": "idempotency-1",
    }
    values.update(changes)
    return KanbanInitiativeApprovalConsumption(**values)


def _prepared_and_approved(conn: sqlite3.Connection):
    evidence = _trusted_evidence()
    host = KanbanInitiativeApprovalHost(evidence)
    conn.execute("BEGIN IMMEDIATE")
    host.prepare(conn, _preparation(), now=1_010)
    host.approve(conn, "approval-1", "second-action-proof", now=1_020)
    conn.commit()
    return evidence, host


def _approval_row(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM write_gate_kanban_approvals WHERE approval_id = ?",
        ("approval-1",),
    ).fetchone()


def test_h12_trusted_gateway_state_mints_redacted_durable_evidence():
    evidence = _trusted_evidence()

    canonical = json.loads(evidence._canonical_for_writegate())

    assert canonical == {
        "connection_id": "connection-1",
        "expires_at": 1_121,
        "issued_at": 1_001,
        "peer_identity": "adrian@tailnet",
        "request_id": "request-1",
        "route": "gateway/tailscale",
        "type": "trusted_authorizer",
        "version": 1,
    }
    assert repr(evidence) == "<TrustedAuthorizerEvidence>"
    assert "adrian" not in repr(evidence)


@pytest.mark.parametrize("ttl", [False, 0, -1, 301, "60"])
def test_f05_trusted_evidence_rejects_invalid_or_overlong_ttl(ttl):
    connection = trusted._record_authenticated_tailscale_connection(
        "gateway/tailscale", "connection-1", "adrian@tailnet", "request-1", 1_000
    )

    with pytest.raises(ValueError, match="ttl_seconds"):
        trusted.TrustedAuthorizerEvidence._from_authenticated_connection(
            connection, issued_at=1_001, ttl_seconds=ttl
        )


def test_u08_caller_material_cannot_forge_registered_authorizer_evidence():
    with pytest.raises((TypeError, ValueError)):
        trusted.TrustedAuthorizerEvidence(
            object(),
            route="gateway/tailscale",
            connection_id="connection-1",
            peer_identity="adrian@tailnet",
            request_id="request-1",
            issued_at=1_001,
            expires_at=1_121,
        )

    forged = trusted.TrustedAuthorizerEvidence(
        trusted._EVIDENCE_MINT,
        route="gateway/tailscale",
        connection_id="connection-1",
        peer_identity="adrian@tailnet",
        request_id="request-1",
        issued_at=1_001,
        expires_at=1_121,
    )
    with pytest.raises(ValueError, match="unregistered"):
        forged._canonical_for_writegate()
    with pytest.raises(KanbanApprovalRejected, match="invalid"):
        KanbanInitiativeApprovalHost(forged)


def test_u05_trusted_evidence_cannot_be_copied_or_pickled():
    evidence = _trusted_evidence()

    with pytest.raises(TypeError, match="pickling not supported"):
        copy.copy(evidence)
    with pytest.raises(TypeError, match="pickling not supported"):
        pickle.dumps(evidence)


def test_writegate_schema_uses_callers_transaction_without_committing():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")

    create_kanban_approval_schema(conn)
    conn.rollback()

    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'write_gate_kanban_approvals'"
    ).fetchone() is None


def test_h02_exact_approval_consumes_in_the_callers_transaction():
    conn = _connection()
    evidence, _ = _prepared_and_approved(conn)
    exact = _consumption(evidence._canonical_for_writegate())

    conn.execute("BEGIN IMMEDIATE")
    consume_approved(conn, exact, now=1_030)
    assert _approval_row(conn)["state"] == "consumed"
    conn.commit()

    row = _approval_row(conn)
    assert row["approval_type"] == APPROVAL_TYPE
    assert row["consumed_mutation_id"] == "mutation-1"
    assert row["consumed_idempotency_ref"] == "idempotency-1"


def test_h02_proposed_creation_target_uses_same_exact_contract():
    conn = _connection()
    evidence = _trusted_evidence()
    host = KanbanInitiativeApprovalHost(evidence)
    preparation = _preparation(
        initiative_id=None,
        proposed_creation_id="proposed-1",
        operation="kanban_create_initiative",
        expected_version=0,
    )
    conn.execute("BEGIN IMMEDIATE")
    host.prepare(conn, preparation, now=1_010)
    host.approve(conn, "approval-1", "second-action-proof", now=1_020)
    consume_approved(
        conn,
        _consumption(
            evidence._canonical_for_writegate(),
            initiative_id=None,
            proposed_creation_id="proposed-1",
            operation="kanban_create_initiative",
            expected_version=0,
        ),
        now=1_030,
    )
    conn.commit()

    assert _approval_row(conn)["state"] == "consumed"


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("request_id", "wrong-request"),
        ("operation", "kanban_archive_initiative"),
        ("initiative_id", "wrong-initiative"),
        ("expected_version", 5),
        ("canonical_digest", "sha256:wrong"),
        ("canonicalization_version", 2),
        ("authorizer_evidence", "wrong-actor"),
        ("session_id", "wrong-session"),
        ("expires_at", 1_201),
        ("consumed_mutation_id", ""),
        ("consumed_idempotency_ref", ""),
    ],
)
def test_u01_every_exact_approval_binding_rejects_independently(field, wrong_value):
    conn = _connection()
    evidence, _ = _prepared_and_approved(conn)
    exact = _consumption(evidence._canonical_for_writegate())

    with pytest.raises(KanbanApprovalRejected):
        changed = replace(exact, **{field: wrong_value})
        conn.execute("BEGIN IMMEDIATE")
        try:
            consume_approved(conn, changed, now=1_030)
        finally:
            conn.rollback()

    assert _approval_row(conn)["state"] == "approved"


@pytest.mark.parametrize("state", ["prepared", "cancelled", "expired", "consumed"])
def test_u01_nonapproved_state_cannot_be_consumed(state):
    conn = _connection()
    evidence, _ = _prepared_and_approved(conn)
    conn.execute(
        "UPDATE write_gate_kanban_approvals SET state = ? WHERE approval_id = ?",
        (state, "approval-1"),
    )

    with pytest.raises(KanbanApprovalRejected, match="exactly one"):
        consume_approved(
            conn, _consumption(evidence._canonical_for_writegate()), now=1_030
        )

    assert _approval_row(conn)["state"] == state


def test_f05_approval_expiry_boundary_is_exclusive():
    conn = _connection()
    evidence, _ = _prepared_and_approved(conn)

    with pytest.raises(KanbanApprovalRejected, match="exactly one"):
        consume_approved(
            conn, _consumption(evidence._canonical_for_writegate()), now=1_200
        )

    assert _approval_row(conn)["state"] == "approved"


def test_f01_outer_rollback_restores_approval_after_consumption():
    conn = _connection()
    evidence, _ = _prepared_and_approved(conn)
    exact = _consumption(evidence._canonical_for_writegate())

    conn.execute("BEGIN IMMEDIATE")
    consume_approved(conn, exact, now=1_030)
    conn.execute("CREATE TABLE governed_mutation (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO governed_mutation VALUES ('mutation-1')")
    conn.rollback()

    assert _approval_row(conn)["state"] == "approved"
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'governed_mutation'"
    ).fetchone() is None


@pytest.mark.parametrize("initial_state", ["prepared", "approved"])
def test_approval_can_be_cancelled_only_from_open_states(initial_state):
    conn = _connection()
    evidence = _trusted_evidence()
    host = KanbanInitiativeApprovalHost(evidence)
    host.prepare(conn, _preparation(), now=1_010)
    if initial_state == "approved":
        host.approve(conn, "approval-1", "second-action-proof", now=1_020)

    host.cancel(conn, "approval-1", "adrian-cancelled", now=1_030)

    row = _approval_row(conn)
    assert row["state"] == "cancelled"
    assert row["cancellation_evidence"] == "adrian-cancelled"
    with pytest.raises(KanbanApprovalRejected, match="exactly one"):
        host.cancel(conn, "approval-1", "repeat", now=1_031)


def test_preparation_requires_exactly_one_nonempty_target():
    with pytest.raises(KanbanApprovalRejected, match="exactly one"):
        _preparation(initiative_id=None, proposed_creation_id=None)
    with pytest.raises(KanbanApprovalRejected, match="exactly one"):
        _preparation(proposed_creation_id="proposed-1")
    with pytest.raises(KanbanApprovalRejected, match="initiative_id"):
        _preparation(initiative_id="   ")


def test_u02_filesystem_approval_type_cannot_satisfy_initiative_consumer():
    conn = _connection()
    evidence, _ = _prepared_and_approved(conn)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        "UPDATE write_gate_kanban_approvals SET approval_type = 'filesystem_lease' "
        "WHERE approval_id = 'approval-1'"
    )

    with pytest.raises(KanbanApprovalRejected, match="exactly one"):
        consume_approved(
            conn, _consumption(evidence._canonical_for_writegate()), now=1_030
        )

    assert _approval_row(conn)["state"] == "approved"


def test_h03_capability_is_exact_registered_redacted_and_one_use(
    capability_module,
):
    binding = _binding(capability_module)
    registry = capability_module.CapabilityRegistry()
    capability = registry._mint_after_admission(binding)

    assert repr(capability) == "<AdrianKanbanCapability>"
    assert "task-1" not in repr(capability)
    assert registry.validate(capability, binding, allow_consumed=False) is True
    assert registry.consume(capability, binding) is True
    assert registry.is_consumed(capability) is True
    assert registry.validate(capability, binding, allow_consumed=True) is True
    with pytest.raises(capability_module.CapabilityRejected, match="consumed"):
        registry.consume(capability, binding)


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("operation", "native_update_task"),
        ("target", "task-2"),
        ("expected_version", 4),
        ("canonical_digest", "sha256:wrong"),
        ("session_id", "session-2"),
        ("workspace_id", "workspace-2"),
        ("plugin_version", "0.2.1"),
        ("protocol_version", "3"),
        ("execution_context", "run-2"),
    ],
)
def test_u06_capability_rejects_each_independent_binding_change(
    capability_module, field, wrong_value
):
    binding = _binding(capability_module)
    registry = capability_module.CapabilityRegistry()
    capability = registry._mint_after_admission(binding)
    changed = replace(binding, **{field: wrong_value})

    with pytest.raises(capability_module.CapabilityRejected, match="mismatch"):
        registry.validate(capability, changed, allow_consumed=False)

    assert registry.is_consumed(capability) is False


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("operation", "  "),
        ("target", ""),
        ("expected_version", True),
        ("expected_version", -1),
        ("canonical_digest", None),
        ("workspace_id", " "),
    ],
)
def test_capability_binding_rejects_malformed_context(
    capability_module, field, wrong_value
):
    with pytest.raises(capability_module.CapabilityRejected):
        _binding(capability_module, **{field: wrong_value})


def test_u05_capability_rejects_forgery_copy_and_pickle(capability_module):
    binding = _binding(capability_module)
    registry = capability_module.CapabilityRegistry()
    real = registry._mint_after_admission(binding)
    forged = capability_module._AdmittedCapability(
        capability_module._CAPABILITY_MINT,
        binding,
        "forged-nonce",
    )

    with pytest.raises(capability_module.CapabilityRejected, match="registered"):
        registry.validate(forged, binding, allow_consumed=False)
    with pytest.raises(TypeError, match="non-serializable"):
        copy.copy(real)
    with pytest.raises(TypeError, match="non-serializable"):
        copy.deepcopy(real)
    with pytest.raises(TypeError, match="non-serializable"):
        pickle.dumps(real)


def test_f03_concurrent_capability_consumers_have_one_winner(capability_module):
    binding = _binding(capability_module)
    registry = capability_module.CapabilityRegistry()
    capability = registry._mint_after_admission(binding)
    barrier = threading.Barrier(3)
    outcomes = []

    def consume():
        barrier.wait()
        try:
            registry.consume(capability, binding)
        except capability_module.CapabilityRejected:
            outcomes.append("rejected")
        else:
            outcomes.append("consumed")

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert outcomes.count("consumed") == 1
    assert outcomes.count("rejected") == 1


def test_h03_scoped_capability_composes_outer_write_and_nested_savepoint(
    provider_modules, tmp_path, monkeypatch
):
    cap_mod = provider_modules["capability"]
    provider_mod = provider_modules["provider"]
    database_path = (tmp_path / "authority" / "kanban.db").resolve()
    database_path.parent.mkdir()
    conn = sqlite3.connect(str(database_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)")
    _select_plugin_authority(tmp_path, monkeypatch, database_path)
    provider = provider_mod.AdrianKanbanAuthorityProvider(str(database_path))
    binding = _binding(cap_mod)
    capability = provider._mint_after_admission(binding)
    kb.clear_authority_providers()
    provider_mod.register_provider(provider)

    try:
        with provider_mod._capability_scope(provider, conn, capability, binding):
            with kb.write_txn(conn):
                conn.execute("INSERT INTO probe VALUES (1, 'outer')")
                with kb.write_txn(conn, allow_nested=True):
                    conn.execute("INSERT INTO probe VALUES (2, 'nested')")

        assert provider.is_consumed(capability) is True
        assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2
        with pytest.raises(kb.AuthorityAdmissionRejected):
            with provider_mod._capability_scope(
                provider, conn, capability, binding
            ):
                pass
    finally:
        kb.clear_authority_providers()
        conn.close()


def test_u04_healthy_selected_provider_without_scope_still_fails_closed(
    provider_modules, tmp_path, monkeypatch
):
    provider_mod = provider_modules["provider"]
    database_path = (tmp_path / "authority" / "kanban.db").resolve()
    database_path.parent.mkdir()
    conn = sqlite3.connect(str(database_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    _select_plugin_authority(tmp_path, monkeypatch, database_path)
    provider = provider_mod.AdrianKanbanAuthorityProvider(str(database_path))
    kb.clear_authority_providers()
    provider_mod.register_provider(provider)

    try:
        with pytest.raises(kb.AuthorityAdmissionRejected, match="fails closed"):
            with kb.write_txn(conn):
                conn.execute("INSERT INTO probe VALUES (1)")
        assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
    finally:
        kb.clear_authority_providers()
        conn.close()


def test_f01_rollback_keeps_capability_spent_and_fresh_admission_can_retry(
    provider_modules, tmp_path, monkeypatch
):
    cap_mod = provider_modules["capability"]
    provider_mod = provider_modules["provider"]
    database_path = (tmp_path / "authority" / "kanban.db").resolve()
    database_path.parent.mkdir()
    conn = sqlite3.connect(str(database_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    _select_plugin_authority(tmp_path, monkeypatch, database_path)
    provider = provider_mod.AdrianKanbanAuthorityProvider(str(database_path))
    binding = _binding(cap_mod)
    spent = provider._mint_after_admission(binding)
    kb.clear_authority_providers()
    provider_mod.register_provider(provider)

    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            with provider_mod._capability_scope(provider, conn, spent, binding):
                with kb.write_txn(conn):
                    conn.execute("INSERT INTO probe VALUES (1)")
                    raise RuntimeError("injected failure")

        assert provider.is_consumed(spent) is True
        assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0

        fresh = provider._mint_after_admission(binding)
        with provider_mod._capability_scope(provider, conn, fresh, binding):
            with kb.write_txn(conn):
                conn.execute("INSERT INTO probe VALUES (1)")
        assert provider.is_consumed(fresh) is True
        assert conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1
    finally:
        kb.clear_authority_providers()
        conn.close()


def test_u06_scope_rejects_different_connection_without_consuming_capability(
    provider_modules, tmp_path, monkeypatch
):
    cap_mod = provider_modules["capability"]
    provider_mod = provider_modules["provider"]
    database_path = (tmp_path / "authority" / "kanban.db").resolve()
    other_path = (tmp_path / "other" / "kanban.db").resolve()
    database_path.parent.mkdir()
    other_path.parent.mkdir()
    conn = sqlite3.connect(str(database_path), isolation_level=None)
    other = sqlite3.connect(str(other_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    other.row_factory = sqlite3.Row
    _select_plugin_authority(tmp_path, monkeypatch, database_path)
    provider = provider_mod.AdrianKanbanAuthorityProvider(str(database_path))
    binding = _binding(cap_mod)
    capability = provider._mint_after_admission(binding)
    kb.clear_authority_providers()
    provider_mod.register_provider(provider)

    try:
        with pytest.raises(kb.AuthorityAdmissionRejected):
            with provider_mod._capability_scope(provider, other, capability, binding):
                pass
        assert provider.is_consumed(capability) is False
    finally:
        kb.clear_authority_providers()
        conn.close()
        other.close()


def test_h16_rejection_envelope_preserves_every_failure_and_remediation(
    provider_modules,
):
    diagnostic = provider_modules["diagnostics"]
    collector = diagnostic.DiagnosticCollector(
        "attempt-1",
        "kanban_transition_initiative",
        diagnostic.Boundary("DEV3/S1", "DEV4/S1"),
    )
    collector.failure(
        diagnostic.FailedCheck(
            "MISSING_HANDOFF",
            "task:t-1",
            "accepted DEV3 handoff",
            "no accepted handoff",
            "accepted task handoff record ID",
            "complete review on the same task card",
            "reviewer",
            "return_route",
        )
    )
    collector.failure(
        diagnostic.FailedCheck(
            "STALE_BASE",
            "workspace:w-1",
            "expected origin/main base",
            "workspace base differs",
            "full Git commit SHA",
            "reconcile the workspace through the DEV3 return route",
            "orchestrator",
            "return_route",
        )
    )
    collector.not_evaluated(
        diagnostic.NotEvaluatedCheck(
            "MERGE_CONTAINMENT",
            ("MISSING_HANDOFF", "STALE_BASE"),
        )
    )

    rejection = collector.rejection()
    payload = rejection.as_dict()

    assert payload["result"] == "REJECTED"
    assert payload["state_changed"] is False
    assert [item["code"] for item in payload["failed_checks"]] == [
        "MISSING_HANDOFF",
        "STALE_BASE",
    ]
    assert payload["not_evaluated_checks"] == [
        {
            "code": "MERGE_CONTAINMENT",
            "requires": ["MISSING_HANDOFF", "STALE_BASE"],
        }
    ]
    rendered = rejection.render()
    assert rendered.startswith("REJECTED — no Kanban state changed")
    assert "complete review on the same task card" in rendered
    assert "reconcile the workspace" in rendered


def test_f14_diagnostic_codes_are_unique_across_all_result_lists(provider_modules):
    diagnostic = provider_modules["diagnostics"]
    collector = diagnostic.DiagnosticCollector(
        "attempt-1", "operation", diagnostic.Boundary("from", "to")
    )
    failure = diagnostic.FailedCheck(
        "DUPLICATE",
        "target",
        "expected",
        "observed",
        "format",
        "safe correction",
        "session_agent",
        "same_operation",
    )
    collector.failure(failure)

    with pytest.raises(ValueError, match="duplicate"):
        collector.failure(failure)
    with pytest.raises(ValueError, match="duplicate"):
        collector.not_evaluated(
            diagnostic.NotEvaluatedCheck("DUPLICATE", ("PREREQUISITE",))
        )
    with pytest.raises(ValueError, match="duplicate"):
        diagnostic.RejectionEnvelope(
            "attempt-1",
            "operation",
            diagnostic.Boundary("from", "to"),
            (failure,),
            (diagnostic.NotEvaluatedCheck("DUPLICATE", ("X",)),),
        )


@pytest.mark.parametrize("actor", ["root", "model", "admin", ""])
def test_diagnostic_rejects_unknown_authority_labels(provider_modules, actor):
    diagnostic = provider_modules["diagnostics"]
    with pytest.raises(ValueError):
        diagnostic.FailedCheck(
            "ACTOR",
            "actor",
            "ratified actor enum",
            "unknown",
            "session_agent | orchestrator | reviewer | adrian | system_operator",
            "use runtime-derived actor evidence",
            actor,
            "not_retryable",
        )
