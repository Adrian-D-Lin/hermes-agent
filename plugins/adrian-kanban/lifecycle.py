import json
import sqlite3
from dataclasses import dataclass
from .contracts import ContractSnapshot, ContractRejected, validate_snapshot

__all__ = [
    "LifecycleRecordRejected",
    "SkillBinding",
    "LifecycleContractRecord",
    "LifecycleContractRepository",
]

_SNAPSHOT_KEYS = (
    "version",
    "contract_id",
    "contract_version",
    "phase",
    "step",
    "initiative_id",
    "segment_id",
    "segment_workspace_id",
    "execution_profile",
    "baseline_refs",
    "governing_source_refs",
    "prior_record_refs",
    "constraints",
    "sequence_group_id",
    "sequence_ordinal",
    "predecessor_ref",
    "release_condition",
    "output_validator",
    "registry_hash",
)

_ARRAY_KEYS = (
    "baseline_refs",
    "governing_source_refs",
    "prior_record_refs",
    "constraints",
)


class LifecycleRecordRejected(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonblank_str(value):
    return isinstance(value, str) and value.strip() != ""


@dataclass(frozen=True)
class SkillBinding:
    skill_id: str
    skill_version: str
    skill_hash: str

    def __post_init__(self):
        for field in ("skill_id", "skill_version", "skill_hash"):
            value = getattr(self, field)
            if not _is_nonblank_str(value):
                raise LifecycleRecordRejected(f"invalid {field}")


@dataclass(frozen=True)
class LifecycleContractRecord:
    task_card_id: int
    task_id: str
    initiative_card_id: int
    snapshot: ContractSnapshot
    skill: SkillBinding
    created_at: int

    def __post_init__(self):
        if not _is_positive_int(self.task_card_id):
            raise LifecycleRecordRejected("invalid task_card_id")
        if not _is_nonblank_str(self.task_id):
            raise LifecycleRecordRejected("invalid task_id")
        if not _is_positive_int(self.initiative_card_id):
            raise LifecycleRecordRejected("invalid initiative_card_id")
        if type(self.snapshot) is not ContractSnapshot:
            raise LifecycleRecordRejected("invalid snapshot")
        if type(self.skill) is not SkillBinding:
            raise LifecycleRecordRejected("invalid skill")
        if not _is_positive_int(self.created_at):
            raise LifecycleRecordRejected("invalid created_at")


class LifecycleContractRepository:
    def __init__(self, conn):
        if type(conn) is not sqlite3.Connection:
            raise LifecycleRecordRejected("invalid connection")
        self._conn = conn

    def attach(self, *, task_id, snapshot, skill, created_at):
        if not _is_nonblank_str(task_id):
            raise LifecycleRecordRejected("invalid task_id")
        if type(snapshot) is not ContractSnapshot:
            raise LifecycleRecordRejected("invalid snapshot")
        if type(skill) is not SkillBinding:
            raise LifecycleRecordRejected("invalid skill")
        if not _is_positive_int(created_at):
            raise LifecycleRecordRejected("invalid created_at")
        if not self._conn.in_transaction:
            raise LifecycleRecordRejected("not in transaction")

        try:
            validate_snapshot(snapshot)
        except ContractRejected:
            raise LifecycleRecordRejected("invalid snapshot") from None

        try:
            cur = self._conn.execute(
                """
                SELECT t.id, t.task_id, t.initiative_id, i.id
                FROM adrian_kanban_cards t
                JOIN adrian_kanban_cards i ON t.initiative_id = i.initiative_id
                WHERE t.card_type = 'task'
                  AND t.task_id = ?
                  AND i.card_type = 'initiative'
                  AND i.task_id IS NULL
                """,
                (task_id,),
            )
            rows = cur.fetchall()
        except sqlite3.Error:
            raise LifecycleRecordRejected("query failed") from None

        if len(rows) != 1:
            raise LifecycleRecordRejected("task resolution failed")

        task_card_id, row_task_id, task_initiative_id, initiative_card_id = rows[0]
        if not _is_positive_int(task_card_id) or not _is_positive_int(initiative_card_id):
            raise LifecycleRecordRejected("invalid card ids")
        if row_task_id != task_id:
            raise LifecycleRecordRejected("task identity mismatch")
        if task_initiative_id != snapshot.initiative_id:
            raise LifecycleRecordRejected("initiative mismatch")

        try:
            cur = self._conn.execute(
                "SELECT 1 FROM task_lifecycle_contracts WHERE task_card_id = ?",
                (task_card_id,),
            )
            if cur.fetchone() is not None:
                raise LifecycleRecordRejected("contract already exists")
        except sqlite3.Error:
            raise LifecycleRecordRejected("query failed") from None

        try:
            cur = self._conn.execute(
                """
                INSERT INTO task_lifecycle_contracts (
                    contract_id, contract_version, step, task_card_id, task_id,
                    initiative_card_id, initiative_id, segment_id, workspace_id,
                    execution_profile, canonical_contract_payload, registry_hash,
                    skill_id, skill_version, skill_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.contract_id,
                    snapshot.contract_version,
                    snapshot.step,
                    task_card_id,
                    task_id,
                    initiative_card_id,
                    snapshot.initiative_id,
                    snapshot.segment_id,
                    snapshot.segment_workspace_id,
                    snapshot.execution_profile,
                    snapshot.canonical_payload(),
                    snapshot.registry_hash,
                    skill.skill_id,
                    skill.skill_version,
                    skill.skill_hash,
                    created_at,
                ),
            )
            if cur.rowcount != 1:
                raise LifecycleRecordRejected("insert failed")
        except sqlite3.Error:
            raise LifecycleRecordRejected("insert failed") from None

        return LifecycleContractRecord(
            task_card_id=task_card_id,
            task_id=task_id,
            initiative_card_id=initiative_card_id,
            snapshot=snapshot,
            skill=skill,
            created_at=created_at,
        )

    def load(self, task_id):
        if not _is_nonblank_str(task_id):
            raise LifecycleRecordRejected("invalid task_id")

        try:
            cur = self._conn.execute(
                """
                SELECT t.id, t.task_id, t.initiative_id, i.id,
                       c.contract_id, c.contract_version, c.step,
                       c.task_card_id, c.task_id, c.initiative_card_id,
                       c.initiative_id, c.segment_id, c.workspace_id,
                       c.execution_profile, c.canonical_contract_payload,
                       c.registry_hash, c.skill_id, c.skill_version,
                       c.skill_hash, c.created_at
                FROM adrian_kanban_cards t
                JOIN adrian_kanban_cards i ON t.initiative_id = i.initiative_id
                LEFT JOIN task_lifecycle_contracts c ON c.task_card_id = t.id
                WHERE t.card_type = 'task'
                  AND t.task_id = ?
                  AND i.card_type = 'initiative'
                  AND i.task_id IS NULL
                """,
                (task_id,),
            )
            rows = cur.fetchall()
        except sqlite3.Error:
            raise LifecycleRecordRejected("query failed") from None

        if len(rows) == 0:
            return None
        if len(rows) > 1:
            raise LifecycleRecordRejected("multiple task rows")

        row = rows[0]
        task_card_id, row_task_id, task_initiative_id, initiative_card_id = row[:4]
        contract_id, contract_version, step, c_task_card_id, c_task_id, c_initiative_card_id, c_initiative_id, c_segment_id, c_workspace_id, c_execution_profile, c_payload, c_registry_hash, c_skill_id, c_skill_version, c_skill_hash, c_created_at = row[4:]

        if c_task_card_id is None:
            return None

        if not _is_positive_int(task_card_id) or not _is_positive_int(initiative_card_id):
            raise LifecycleRecordRejected("invalid card ids")
        if row_task_id != task_id:
            raise LifecycleRecordRejected("task identity mismatch")

        if c_task_card_id != task_card_id:
            raise LifecycleRecordRejected("task_card_id mismatch")
        if c_task_id != task_id:
            raise LifecycleRecordRejected("task_id mismatch")
        if c_initiative_card_id != initiative_card_id:
            raise LifecycleRecordRejected("initiative_card_id mismatch")
        if c_initiative_id != task_initiative_id:
            raise LifecycleRecordRejected("initiative_id mismatch")

        try:
            payload_obj = json.loads(c_payload)
        except (json.JSONDecodeError, TypeError):
            raise LifecycleRecordRejected("invalid payload") from None

        if not isinstance(payload_obj, dict):
            raise LifecycleRecordRejected("invalid payload")

        if set(payload_obj.keys()) != set(_SNAPSHOT_KEYS):
            raise LifecycleRecordRejected("invalid payload keys")

        for key in _ARRAY_KEYS:
            if not isinstance(payload_obj[key], list):
                raise LifecycleRecordRejected(f"invalid {key}")
            payload_obj[key] = tuple(payload_obj[key])

        try:
            snapshot = ContractSnapshot(**payload_obj)
        except (ContractRejected, TypeError, ValueError):
            raise LifecycleRecordRejected("invalid snapshot") from None

        if type(snapshot) is not ContractSnapshot:
            raise LifecycleRecordRejected("invalid snapshot")

        if snapshot.canonical_payload() != c_payload:
            raise LifecycleRecordRejected("payload mismatch")

        try:
            validate_snapshot(snapshot)
        except ContractRejected:
            raise LifecycleRecordRejected("invalid snapshot") from None

        if snapshot.contract_id != contract_id:
            raise LifecycleRecordRejected("contract_id mismatch")
        if str(snapshot.contract_version) != contract_version:
            raise LifecycleRecordRejected("contract_version mismatch")
        if snapshot.step != step:
            raise LifecycleRecordRejected("step mismatch")
        if snapshot.initiative_id != c_initiative_id:
            raise LifecycleRecordRejected("initiative_id mismatch")
        if snapshot.segment_id != c_segment_id:
            raise LifecycleRecordRejected("segment_id mismatch")
        if snapshot.segment_workspace_id != c_workspace_id:
            raise LifecycleRecordRejected("workspace_id mismatch")
        if snapshot.execution_profile != c_execution_profile:
            raise LifecycleRecordRejected("execution_profile mismatch")
        if snapshot.registry_hash != c_registry_hash:
            raise LifecycleRecordRejected("registry_hash mismatch")

        try:
            skill = SkillBinding(
                skill_id=c_skill_id,
                skill_version=c_skill_version,
                skill_hash=c_skill_hash,
            )
        except LifecycleRecordRejected:
            raise

        if not _is_positive_int(c_created_at):
            raise LifecycleRecordRejected("invalid created_at")

        return LifecycleContractRecord(
            task_card_id=task_card_id,
            task_id=task_id,
            initiative_card_id=initiative_card_id,
            snapshot=snapshot,
            skill=skill,
            created_at=c_created_at,
        )
