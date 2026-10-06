"""Portable coordinator declarations, distinct from the host tool broker."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import InvalidRequestError


def strict_json(text: str | bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise InvalidRequestError(f"Invalid coordinator JSON: {exc}") from exc


@dataclass(frozen=True)
class HostCoordinator:
    """A host's explicit attestation; environment detection alone is insufficient."""

    coordinator_id: str
    default_provider: str = "host"
    native_fallback: bool = True
    fresh_context: bool = False
    model_selection: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.coordinator_id, str) or not self.coordinator_id.strip():
            raise InvalidRequestError("coordinator_id must be a non-empty string.")
        if not isinstance(self.default_provider, str) or not self.default_provider.strip():
            raise InvalidRequestError("default_provider must be a non-empty string.")
        for name in ("native_fallback", "fresh_context", "model_selection"):
            if type(getattr(self, name)) is not bool:
                raise InvalidRequestError(f"host coordinator {name} must be boolean.")

    def to_document(self) -> dict[str, Any]:
        return {
            "coordinator_id": self.coordinator_id,
            "default_provider": self.default_provider,
            "native_fallback": self.native_fallback,
            "fresh_context": self.fresh_context,
            "model_selection": self.model_selection,
        }

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> HostCoordinator | None:
        raw = (os.environ if env is None else env).get("AC_LLM_HOST_COORDINATOR")
        if not raw:
            return None
        value = strict_json(raw)
        allowed = {"coordinator_id", "default_provider", "native_fallback", "fresh_context", "model_selection"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise InvalidRequestError("Invalid AC_LLM_HOST_COORDINATOR fields.")
        try:
            return cls(**value)
        except TypeError as exc:
            raise InvalidRequestError("AC_LLM_HOST_COORDINATOR requires coordinator_id.") from exc
