from __future__ import annotations

from dataclasses import dataclass

ALLOWED_ACTORS = frozenset(
    {"session_agent", "orchestrator", "reviewer", "adrian", "system_operator"}
)
ALLOWED_RETRIES = frozenset(
    {"same_operation", "return_route", "human_decision", "not_retryable"}
)


def _nonblank(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{field_name} must be a nonblank string")
    return trimmed


@dataclass(frozen=True)
class Boundary:
    source: str
    destination: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _nonblank(self.source, "source"))
        object.__setattr__(
            self,
            "destination",
            _nonblank(self.destination, "destination"),
        )

    def as_dict(self) -> dict:
        return {"from": self.source, "to": self.destination}


@dataclass(frozen=True)
class FailedCheck:
    code: str
    target: str
    expected: str
    observed: str
    accepted_format: str
    remediation: str
    responsible_actor: str
    retry: str

    def __post_init__(self) -> None:
        for name in (
            "code",
            "target",
            "expected",
            "observed",
            "accepted_format",
            "remediation",
            "responsible_actor",
            "retry",
        ):
            object.__setattr__(self, name, _nonblank(getattr(self, name), name))
        if self.responsible_actor not in ALLOWED_ACTORS:
            raise ValueError("responsible_actor must be an allowed actor")
        if self.retry not in ALLOWED_RETRIES:
            raise ValueError("retry must be an allowed retry value")

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "target": self.target,
            "expected": self.expected,
            "observed": self.observed,
            "accepted_format": self.accepted_format,
            "remediation": self.remediation,
            "responsible_actor": self.responsible_actor,
            "retry": self.retry,
        }


@dataclass(frozen=True)
class NotEvaluatedCheck:
    code: str
    requires: tuple[str, ...]

    def __post_init__(self) -> None:
        code = _nonblank(self.code, "code")
        if not self.requires:
            raise ValueError("requires must be nonempty")
        cleaned = tuple(_nonblank(value, "requires") for value in self.requires)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("requires must not contain duplicates")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "requires", cleaned)

    def as_dict(self) -> dict:
        return {"code": self.code, "requires": list(self.requires)}


@dataclass(frozen=True)
class RejectionEnvelope:
    attempt_id: str
    operation: str
    boundary: Boundary
    failed_checks: tuple[FailedCheck, ...] = ()
    not_evaluated_checks: tuple[NotEvaluatedCheck, ...] = ()

    def __post_init__(self) -> None:
        attempt_id = _nonblank(self.attempt_id, "attempt_id")
        operation = _nonblank(self.operation, "operation")
        if type(self.boundary) is not Boundary:
            raise ValueError("boundary must be a Boundary")
        failed_checks = tuple(self.failed_checks)
        not_evaluated_checks = tuple(self.not_evaluated_checks)
        if not failed_checks and not not_evaluated_checks:
            raise ValueError("at least one failed or not-evaluated check is required")
        for check in failed_checks:
            if type(check) is not FailedCheck:
                raise ValueError("failed_checks must contain only FailedCheck")
        for check in not_evaluated_checks:
            if type(check) is not NotEvaluatedCheck:
                raise ValueError(
                    "not_evaluated_checks must contain only NotEvaluatedCheck"
                )
        codes = [check.code for check in failed_checks]
        codes.extend(check.code for check in not_evaluated_checks)
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate check code")
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "failed_checks", failed_checks)
        object.__setattr__(self, "not_evaluated_checks", not_evaluated_checks)

    @property
    def result(self) -> str:
        return "REJECTED"

    @property
    def state_changed(self) -> bool:
        return False

    def as_dict(self) -> dict:
        return {
            "result": self.result,
            "state_changed": self.state_changed,
            "attempt_id": self.attempt_id,
            "operation": self.operation,
            "boundary": self.boundary.as_dict(),
            "failed_checks": [check.as_dict() for check in self.failed_checks],
            "not_evaluated_checks": [
                check.as_dict() for check in self.not_evaluated_checks
            ],
        }

    def render(self) -> str:
        lines = [
            "REJECTED — no Kanban state changed",
            f"operation: {self.operation}",
            f"boundary: {self.boundary.source} -> {self.boundary.destination}",
            f"attempt_id: {self.attempt_id}",
        ]
        for check in self.failed_checks:
            lines.extend(
                (
                    f"failed: {check.code}",
                    f"  target: {check.target}",
                    f"  expected: {check.expected}",
                    f"  observed: {check.observed}",
                    f"  accepted_format: {check.accepted_format}",
                    f"  remediation: {check.remediation}",
                    f"  responsible_actor: {check.responsible_actor}",
                    f"  retry: {check.retry}",
                )
            )
        for check in self.not_evaluated_checks:
            lines.extend(
                (
                    f"not_evaluated: {check.code}",
                    f"  requires: {', '.join(check.requires)}",
                )
            )
        return "\n".join(lines)


class DiagnosticCollector:
    def __init__(self, attempt_id: str, operation: str, boundary: Boundary) -> None:
        self._attempt_id = _nonblank(attempt_id, "attempt_id")
        self._operation = _nonblank(operation, "operation")
        if type(boundary) is not Boundary:
            raise ValueError("boundary must be a Boundary")
        self._boundary = boundary
        self._failed_checks: list[FailedCheck] = []
        self._not_evaluated_checks: list[NotEvaluatedCheck] = []

    def _has_code(self, code: str) -> bool:
        return any(check.code == code for check in self._failed_checks) or any(
            check.code == code for check in self._not_evaluated_checks
        )

    def failure(self, check: FailedCheck) -> "DiagnosticCollector":
        if type(check) is not FailedCheck:
            raise ValueError("check must be a FailedCheck")
        if self._has_code(check.code):
            raise ValueError("duplicate check code")
        self._failed_checks.append(check)
        return self

    def not_evaluated(self, check: NotEvaluatedCheck) -> "DiagnosticCollector":
        if type(check) is not NotEvaluatedCheck:
            raise ValueError("check must be a NotEvaluatedCheck")
        if self._has_code(check.code):
            raise ValueError("duplicate check code")
        self._not_evaluated_checks.append(check)
        return self

    @property
    def has_findings(self) -> bool:
        return bool(self._failed_checks or self._not_evaluated_checks)

    def rejection(self) -> RejectionEnvelope:
        if not self.has_findings:
            raise ValueError("no findings to build a rejection envelope")
        return RejectionEnvelope(
            attempt_id=self._attempt_id,
            operation=self._operation,
            boundary=self._boundary,
            failed_checks=tuple(self._failed_checks),
            not_evaluated_checks=tuple(self._not_evaluated_checks),
        )


__all__ = [
    "Boundary",
    "FailedCheck",
    "NotEvaluatedCheck",
    "RejectionEnvelope",
    "DiagnosticCollector",
]
