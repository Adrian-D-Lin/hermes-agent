import secrets
import threading
import weakref
from dataclasses import InitVar, dataclass, field
from typing import Optional

__all__ = ["CapabilityBinding", "CapabilityRegistry", "CapabilityRejected"]


class CapabilityRejected(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CapabilityBinding:
    operation: str
    target: str
    expected_version: int
    canonical_digest: str
    session_id: str
    workspace_id: Optional[str]
    plugin_version: str
    protocol_version: str
    execution_context: str
    actor_profile: Optional[str] = None

    def __post_init__(self):
        required_strings = [
            self.operation,
            self.target,
            self.canonical_digest,
            self.session_id,
            self.plugin_version,
            self.protocol_version,
            self.execution_context,
        ]
        for value in required_strings:
            if not isinstance(value, str) or not value.strip():
                raise CapabilityRejected("invalid required string")

        if (
            isinstance(self.expected_version, bool)
            or not isinstance(self.expected_version, int)
            or self.expected_version < 0
        ):
            raise CapabilityRejected("invalid expected_version")

        if self.workspace_id is not None:
            if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
                raise CapabilityRejected("invalid workspace_id")
        if self.actor_profile is not None and not (
            type(self.actor_profile) is str and self.actor_profile.strip()
        ):
            raise CapabilityRejected("invalid actor_profile")


_CAPABILITY_MINT = object()


@dataclass(frozen=True, eq=False)
class _AdmittedCapability:
    _mint: InitVar[object]
    binding: CapabilityBinding
    _nonce: str = field(repr=False)

    def __post_init__(self, _mint: object):
        if _mint is not _CAPABILITY_MINT:
            raise CapabilityRejected("invalid mint token")
        if type(self.binding) is not CapabilityBinding:
            raise CapabilityRejected("invalid binding type")
        if not isinstance(self._nonce, str) or not self._nonce.strip():
            raise CapabilityRejected("invalid nonce")

    def __repr__(self):
        return "<AdrianKanbanCapability>"

    def __reduce__(self):
        raise TypeError("capability is non-serializable")

    def __copy__(self):
        raise TypeError("capability is non-serializable")

    def __deepcopy__(self, memo):
        raise TypeError("capability is non-serializable")


class CapabilityRegistry:
    def __init__(self):
        self._registry = weakref.WeakKeyDictionary()
        self._lock = threading.Lock()

    def _mint_after_admission(self, binding: CapabilityBinding) -> _AdmittedCapability:
        if type(binding) is not CapabilityBinding:
            raise CapabilityRejected("invalid binding type")
        nonce = secrets.token_hex(32)
        token = _AdmittedCapability(
            _mint=_CAPABILITY_MINT,
            binding=binding,
            _nonce=nonce,
        )
        with self._lock:
            self._registry[token] = False
        return token

    def validate(self, capability, context, *, allow_consumed: bool) -> bool:
        if type(capability) is not _AdmittedCapability:
            raise CapabilityRejected("invalid capability type")
        if type(context) is not CapabilityBinding:
            raise CapabilityRejected("invalid context type")
        if not isinstance(allow_consumed, bool):
            raise CapabilityRejected("invalid allow_consumed type")

        with self._lock:
            if capability not in self._registry:
                raise CapabilityRejected("capability not registered")
            if context != capability.binding:
                raise CapabilityRejected("binding mismatch")
            if self._registry[capability] and not allow_consumed:
                raise CapabilityRejected("capability consumed")
        return True

    def consume(self, capability, context) -> bool:
        if type(capability) is not _AdmittedCapability:
            raise CapabilityRejected("invalid capability type")
        if type(context) is not CapabilityBinding:
            raise CapabilityRejected("invalid context type")

        with self._lock:
            if capability not in self._registry:
                raise CapabilityRejected("capability not registered")
            if context != capability.binding:
                raise CapabilityRejected("binding mismatch")
            if self._registry[capability]:
                raise CapabilityRejected("capability already consumed")
            self._registry[capability] = True
        return True

    def is_consumed(self, capability) -> bool:
        if type(capability) is not _AdmittedCapability:
            raise CapabilityRejected("invalid capability type")
        with self._lock:
            if capability not in self._registry:
                raise CapabilityRejected("capability not registered")
            return self._registry[capability]
