import json
from dataclasses import dataclass
from gateway.trusted_authorizer_evidence import TrustedAuthorizerEvidence

__all__ = [
    "ActorEvidenceRejected",
    "ValidatedAuthorizerEvidence",
    "validate_authorizer_evidence",
]


class ActorEvidenceRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ValidatedAuthorizerEvidence:
    evidence_type: str
    evidence_version: int
    route: str
    connection_id: str
    peer_identity: str
    request_id: str
    issued_at: int
    expires_at: int
    canonical_payload: str

    def __post_init__(self):
        if (
            not isinstance(self.evidence_type, str)
            or self.evidence_type != "trusted_authorizer"
        ):
            raise ActorEvidenceRejected("invalid_evidence_type")
        if (
            isinstance(self.evidence_version, bool)
            or not isinstance(self.evidence_version, int)
            or self.evidence_version != 1
        ):
            raise ActorEvidenceRejected("invalid_evidence_version")
        for field in (
            self.route,
            self.connection_id,
            self.peer_identity,
            self.request_id,
        ):
            if not isinstance(field, str) or not field.strip():
                raise ActorEvidenceRejected("invalid_identity_or_request")
        for field in (self.issued_at, self.expires_at):
            if isinstance(field, bool) or not isinstance(field, int) or field <= 0:
                raise ActorEvidenceRejected("invalid_time")
        if self.expires_at <= self.issued_at:
            raise ActorEvidenceRejected("invalid_time_range")
        expected_payload = json.dumps(
            {
                "type": self.evidence_type,
                "version": self.evidence_version,
                "route": self.route,
                "connection_id": self.connection_id,
                "peer_identity": self.peer_identity,
                "request_id": self.request_id,
                "issued_at": self.issued_at,
                "expires_at": self.expires_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if self.canonical_payload != expected_payload:
            raise ActorEvidenceRejected("invalid_canonical_payload")


def _object_pairs_hook(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ActorEvidenceRejected("duplicate_key")
        d[k] = v
    return d


def validate_authorizer_evidence(
    evidence, *, expected_request_id: str, now: int
) -> ValidatedAuthorizerEvidence:
    if type(evidence) is not TrustedAuthorizerEvidence:
        raise ActorEvidenceRejected("invalid_evidence_type")
    if not isinstance(expected_request_id, str) or not expected_request_id.strip():
        raise ActorEvidenceRejected("invalid_expected_request_id")
    if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
        raise ActorEvidenceRejected("invalid_now")

    try:
        raw = evidence._canonical_for_writegate()
    except Exception:
        raise ActorEvidenceRejected("canonicalization_failed") from None

    if not isinstance(raw, str):
        raise ActorEvidenceRejected("invalid_canonical_payload")

    try:
        parsed = json.loads(raw, object_pairs_hook=_object_pairs_hook)
    except ActorEvidenceRejected:
        raise
    except Exception:
        raise ActorEvidenceRejected("invalid_json") from None

    if not isinstance(parsed, dict):
        raise ActorEvidenceRejected("invalid_json_structure")

    required_keys = {
        "type",
        "version",
        "route",
        "connection_id",
        "peer_identity",
        "request_id",
        "issued_at",
        "expires_at",
    }
    if set(parsed.keys()) != required_keys:
        raise ActorEvidenceRejected("invalid_keys")

    expected_payload = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    if raw != expected_payload:
        raise ActorEvidenceRejected("invalid_canonical_payload")

    if parsed["type"] != "trusted_authorizer":
        raise ActorEvidenceRejected("invalid_evidence_type")
    if (
        isinstance(parsed["version"], bool)
        or not isinstance(parsed["version"], int)
        or parsed["version"] != 1
    ):
        raise ActorEvidenceRejected("invalid_evidence_version")
    for key in ("route", "connection_id", "peer_identity", "request_id"):
        if not isinstance(parsed[key], str) or not parsed[key].strip():
            raise ActorEvidenceRejected("invalid_identity_or_request")
    for key in ("issued_at", "expires_at"):
        if (
            isinstance(parsed[key], bool)
            or not isinstance(parsed[key], int)
            or parsed[key] <= 0
        ):
            raise ActorEvidenceRejected("invalid_time")
    if parsed["expires_at"] <= parsed["issued_at"]:
        raise ActorEvidenceRejected("invalid_time_range")
    if parsed["issued_at"] > now:
        raise ActorEvidenceRejected("invalid_time_range")
    if parsed["expires_at"] <= now:
        raise ActorEvidenceRejected("invalid_time_range")
    if parsed["request_id"] != expected_request_id:
        raise ActorEvidenceRejected("request_id_mismatch")

    return ValidatedAuthorizerEvidence(
        evidence_type=parsed["type"],
        evidence_version=parsed["version"],
        route=parsed["route"],
        connection_id=parsed["connection_id"],
        peer_identity=parsed["peer_identity"],
        request_id=parsed["request_id"],
        issued_at=parsed["issued_at"],
        expires_at=parsed["expires_at"],
        canonical_payload=raw,
    )
