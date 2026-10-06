"""Non-blocking material handoff to an explicitly configured agent host."""

from __future__ import annotations

from typing import Any

from ..host_execution import HostCoordinator
from .base import (
    IsolationMode, ProviderCapabilities, ProviderDiagnostic, ProviderExecution,
    ProviderRequest, ProviderTerminalKind, StructuredOutputMode, UsageAvailability,
)


class HostAdapter:
    name = "host"
    compatibility_version = "host-handoff.v1"

    def __init__(self, coordinator: HostCoordinator | None = None) -> None:
        self.coordinator = coordinator if coordinator is not None else HostCoordinator.from_environment()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            native_resume=False,
            structured_output=StructuredOutputMode.PROMPT,
            usage=UsageAvailability.UNAVAILABLE,
            config_isolation=IsolationMode.EXPLICIT,
            tool_isolation=IsolationMode.INHERITED,
            cooperative_stop=True,
            provider_persistence=False,
        )

    def doctor(self) -> ProviderDiagnostic:
        return ProviderDiagnostic(
            self.name, self.coordinator is not None, None,
            {"coordinator": None if self.coordinator is None else self.coordinator.to_document(),
             "native_session_resume": False,
             "next_step": "Configure AC_LLM_HOST_COORDINATOR only when an agent will export, submit, and resume tasks."},
        )

    def start(self, request: ProviderRequest, observer: Any, stop: Any) -> ProviderExecution:
        stop.raise_if_requested()
        return ProviderExecution(ProviderTerminalKind.AWAITING_HOST)

    def resume(self, handle: Any, request: Any, observer: Any, stop: Any) -> ProviderExecution:
        raise ValueError("Host execution has no native model-session handle.")
