"""Phase D1 fixed result preparation and locked admission for Adrian Kanban."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .published_artifact import VerifiedArtifact, verify_published_artifact


@dataclass(frozen=True)
class PreparedD1Result:
    initiative_id: str
    update_digest: str
    draft: VerifiedArtifact
    prior_d2_result_ref: str | None


def _nonblank(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")
    return value


def _validate_record_list(
    records: object,
    field: str,
    keys: tuple[str, ...],
    unique_key: str,
) -> None:
    if not isinstance(records, list):
        raise ValueError(f"{field} must be a list")
    seen: set[str] = set()
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise ValueError(f"{field}[{i}] must be a dict")
        if set(rec.keys()) != set(keys):
            raise ValueError(f"{field}[{i}] must have exactly keys {', '.join(keys)}")
        for k in keys:
            _nonblank(rec[k], f"{field}[{i}].{k}")
        ref = rec[unique_key]
        if ref in seen:
            raise ValueError(f"{field} has duplicate {unique_key}")
        seen.add(ref)


def prepare_d1_result(
    initiative_id: object,
    update: object,
    read_published_blob,
) -> PreparedD1Result:
    _nonblank(initiative_id, "initiative_id")
    if not isinstance(update, dict):
        raise ValueError("update must be a dict")
    if update.get("phase") != "D1":
        raise ValueError("update.phase must be 'D1'")
    if update.get("segment_id") is not None:
        raise ValueError("update.segment_id must be None")
    if update.get("result_kind") != "phase_close":
        raise ValueError("update.result_kind must be 'phase_close'")

    result = update.get("result")
    if not isinstance(result, dict):
        raise ValueError("update.result must be a dict")
    expected_keys = {
        "draft_ref",
        "open_questions",
        "revision_findings",
        "prior_d2_result_ref",
        "next_route",
    }
    if set(result.keys()) != expected_keys:
        raise ValueError(
            "update.result must have exactly keys "
            "draft_ref, open_questions, revision_findings, prior_d2_result_ref, next_route"
        )

    if result["next_route"] != "D2":
        raise ValueError("update.result.next_route must be 'D2'")

    _validate_record_list(
        result["open_questions"],
        "update.result.open_questions",
        ("item_id", "question", "context"),
        "item_id",
    )
    _validate_record_list(
        result["revision_findings"],
        "update.result.revision_findings",
        ("finding_ref", "disposition", "rationale"),
        "finding_ref",
    )

    prior_ref = result["prior_d2_result_ref"]
    if prior_ref is not None:
        _nonblank(prior_ref, "update.result.prior_d2_result_ref")
    else:
        if result["revision_findings"]:
            raise ValueError(
                "update.result.revision_findings must be empty when prior_d2_result_ref is None"
            )

    draft = verify_published_artifact(result["draft_ref"], read_published_blob)

    canonical = json.dumps(
        update,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()

    return PreparedD1Result(
        initiative_id=initiative_id,
        update_digest=digest,
        draft=draft,
        prior_d2_result_ref=prior_ref,
    )


def admit_d1_result(
    context,
    initiative_card_id: int,
    initiative_id: object,
    update: object,
    prepared: object,
) -> None:
    if type(prepared) is not PreparedD1Result:
        raise ValueError("prepared must be a PreparedD1Result instance")
    _nonblank(initiative_id, "initiative_id")
    if prepared.initiative_id != initiative_id:
        raise ValueError("initiative_id does not match prepared result")
    if not isinstance(update, dict):
        raise ValueError("update must be a dict")

    canonical = json.dumps(
        update,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if digest != prepared.update_digest:
        raise ValueError("update digest does not match prepared result")

    result = update.get("result")
    if not isinstance(result, dict):
        raise ValueError("update.result must be a dict")
    revision_findings = result.get("revision_findings", [])
    prior_ref = result.get("prior_d2_result_ref")

    conn = context.connection
    row = conn.execute(
        """
        SELECT result_id, canonical_payload, contract_id, contract_version
        FROM initiative_phase_results
        WHERE initiative_card_id = ?
          AND initiative_id = ?
          AND phase = 'D2'
          AND segment_id IS NULL
          AND result_kind = 'phase_close'
          AND accepted = 1
        ORDER BY created_at DESC, rowid DESC
        LIMIT 1
        """,
        (initiative_card_id, initiative_id),
    ).fetchone()

    if row is None:
        if prior_ref is not None:
            raise ValueError(
                "prior_d2_result_ref must be None when no prior D2 result exists"
            )
        if revision_findings:
            raise ValueError(
                "revision_findings must be empty when no prior D2 result exists"
            )
        return

    if (
        row["contract_id"] != "adrian-kanban.lifecycle.d2"
        or row["contract_version"] != "1"
    ):
        raise ValueError("prior D2 result has unexpected contract")

    if prior_ref != row["result_id"]:
        raise ValueError("prior_d2_result_ref does not match latest accepted D2 result")

    try:
        payload = json.loads(row["canonical_payload"])
    except (json.JSONDecodeError, TypeError):
        raise ValueError("prior D2 canonical_payload is not valid JSON") from None

    if not isinstance(payload, dict):
        raise ValueError("prior D2 canonical_payload must be a dict")
    dispositions = payload.get("finding_dispositions")
    if not isinstance(dispositions, list):
        raise ValueError(
            "prior D2 canonical_payload must contain finding_dispositions list"
        )

    stored_refs: set[str] = set()
    for i, rec in enumerate(dispositions):
        if not isinstance(rec, dict):
            raise ValueError(f"prior D2 finding_dispositions[{i}] must be a dict")
        ref = rec.get("finding_ref")
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(
                f"prior D2 finding_dispositions[{i}].finding_ref must be nonblank"
            )
        if ref in stored_refs:
            raise ValueError("prior D2 finding_dispositions has duplicate finding_ref")
        stored_refs.add(ref)

    submitted_refs: set[str] = set()
    for rec in revision_findings:
        ref = rec["finding_ref"]
        if ref in submitted_refs:
            raise ValueError("revision_findings has duplicate finding_ref")
        submitted_refs.add(ref)

    if submitted_refs != stored_refs:
        raise ValueError(
            "revision_findings finding_ref set must exactly match prior D2 finding_dispositions"
        )
