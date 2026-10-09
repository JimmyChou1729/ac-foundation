"""Resolve a new durable recipe through the same route as LLM execution."""
from __future__ import annotations

from dataclasses import replace

from .config import ResolvedModelSelection, resolve_model_selection
from .errors import InvalidRequestError
from .host_execution import HostCoordinator
from .providers.registry import ProviderRegistry, default_registry
from .providers.host import HostAdapter
from .request import LLMExecutionOptions, ModelSelection, SessionRef


def resolve_execution_model(
    selection: ModelSelection,
    *,
    options: LLMExecutionOptions | None = None,
    registry: ProviderRegistry | None = None,
    session: SessionRef | None = None,
) -> ResolvedModelSelection:
    """Resolve before creating a run; never launch a generation or rebind a run.

    Unlike config-only model defaults, this observes the coordinator declaration
    and native prelaunch availability. Existing runs retain their frozen recipe.
    """
    registry = default_registry() if registry is None else registry

    def adapter(name, execution_options):
        candidate = registry.create(name)
        if isinstance(candidate, HostAdapter) and execution_options.host_coordinator is not None:
            return HostAdapter(execution_options.host_coordinator)
        return candidate

    options = LLMExecutionOptions() if options is None else options
    if options.host_coordinator is None:
        coordinator = HostCoordinator.from_environment()
        if coordinator is not None:
            options = replace(options, host_coordinator=coordinator)
    available = registry.names()
    coordinator = options.host_coordinator
    if coordinator is not None and "host" in available:
        preferred = coordinator.default_provider if selection.provider == "auto" else selection.provider
        if selection.provider == "auto" and preferred not in available:
            raise InvalidRequestError(f"Coordinator default provider is not registered: {preferred}")
        if preferred == "host":
            return resolve_model_selection(replace(selection, provider="host"), available=available)
        if preferred in available and session is None and coordinator.native_fallback:
            diagnostic = adapter(preferred, options).doctor()
            if not diagnostic.available and diagnostic.prelaunch_unavailable:
                return resolve_model_selection(replace(selection, provider="host"), available=available)
        if selection.provider == "auto" and preferred in available:
            return resolve_model_selection(replace(selection, provider=preferred), available=available)
    if selection.provider == "auto":
        healthy: list[str] = []
        for name in available:
            try:
                if adapter(name, options).doctor().available:
                    healthy.append(name)
            except Exception:
                continue
        if healthy:
            available = tuple(healthy)
    return resolve_model_selection(selection, available=available)
