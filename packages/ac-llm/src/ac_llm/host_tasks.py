"""Durable model-task handoff and public coordinator operations."""

from __future__ import annotations

import base64
import copy
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from ac_jobs import (
    AtomicStateStore, CorruptStateError, FileLease, RevisionConflictError, RunContext,
    RunRepository, RunStatus, StateConflictError, canonical_json_bytes,
    atomic_write_bytes,
    validate_simple_id,
)
from jsonschema import Draft202012Validator

from .errors import IdempotencyConflictError, InvalidRequestError, OutputInvalidError
from .host_execution import strict_json
from .output import CandidateMaterial
from .providers.base import ProviderExecution, ProviderTerminalKind, ProviderUsage

TASK_PROTOCOL = "ac.llm.host_task.v1"
RESPONSE_PROTOCOL = "ac.llm.host_response.v1"


@dataclass(frozen=True)
class _Receipt:
    revision: int
    task: Mapping[str, Any]
    response: Mapping[str, Any] | None = None


class _ReceiptContract:
    schema_version = "ac.llm.host_receipt.v1"

    def encode(self, value: _Receipt) -> Mapping[str, Any]:
        return {"revision": value.revision, "task": dict(value.task), "response": value.response}

    def decode(self, value: Mapping[str, Any]) -> _Receipt:
        if set(value) != {"revision", "task", "response"} or type(value["revision"]) is not int:
            raise CorruptStateError("Invalid host receipt fields.")
        task = value["task"]
        if not isinstance(task, dict) or task.get("schema_version") != TASK_PROTOCOL:
            raise CorruptStateError("Invalid host task protocol.")
        try:
            validate_simple_id(task["task_id"], label="host task id")
            expected = _identity(task)
            if task["task_id"] != expected:
                raise ValueError("host task identity mismatch")
            if _digest(task["request"]) != task["request_sha256"]:
                raise ValueError("host task input digest mismatch")
            response = value["response"]
            if response is not None:
                response = _response(task, response)
        except (KeyError, TypeError, ValueError, InvalidRequestError, OutputInvalidError) as exc:
            raise CorruptStateError(f"Invalid host receipt: {exc}") from exc
        return _Receipt(value["revision"], task, response)

    def validate_transition(self, previous: _Receipt | None, value: _Receipt) -> None:
        self.decode(self.encode(value))
        if previous is None:
            if value.revision != 0 or value.response is not None:
                raise CorruptStateError("A host receipt starts without a response.")
        elif value.task != previous.task or previous.response is not None or value.response is None:
            raise CorruptStateError("A host receipt accepts exactly one immutable response.")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _identity(task: Mapping[str, Any]) -> str:
    binding = {key: task[key] for key in (
        "schema_version", "run_id", "llm_task_id", "semantic_key_sha256",
        "generation", "host_turn_round", "recovery_epoch", "request_sha256",
    )}
    return f"host-{_digest(binding)}"


def _store(context: RunContext, task_id: str, *, epoch: int | None = None) -> AtomicStateStore[_Receipt]:
    validate_simple_id(task_id, label="host task id")
    epoch = context.recovery_epoch if epoch is None else epoch
    return AtomicStateStore(
        context.run_directory / "host-tasks" / f"epoch-{epoch}" / f"{task_id}.json",
        _ReceiptContract(),
    )


def _task_state(context: RunContext, task: Mapping[str, Any]) -> Any:
    from .executor import LLMTaskExecutor
    from .recovery import TaskStateContract

    return context.state(LLMTaskExecutor._task_namespace(task["llm_task_id"]), TaskStateContract()).read()


def _active(context: RunContext, task: Mapping[str, Any]) -> Any:
    if task["run_id"] != context.run_id or task["recovery_epoch"] != context.recovery_epoch:
        raise InvalidRequestError("The host task belongs to another run or recovery epoch.")
    state = _task_state(context, task)
    if state is None or state.semantic_key.sha256 != task["semantic_key_sha256"]:
        raise InvalidRequestError("The host task no longer matches the LLM request.")
    if state.accepted is not None or state.current.generation != task["generation"]:
        raise InvalidRequestError("The host task is completed or superseded.")
    if state.host_turn_round != task["host_turn_round"]:
        raise InvalidRequestError("The host task targets an old host turn.")
    return state


def _response(task: Mapping[str, Any], value: Any) -> dict[str, Any]:
    required = {"schema_version", "task_id", "request_sha256", "actor", "output"}
    optional = {"actual_model", "reasoning_effort", "isolation", "usage"}
    if not isinstance(value, Mapping) or not required <= set(value) or set(value) - required - optional:
        raise InvalidRequestError("Invalid host response fields.")
    if value["schema_version"] != RESPONSE_PROTOCOL:
        raise InvalidRequestError("Unsupported host response protocol.")
    if value["task_id"] != task["task_id"] or value["request_sha256"] != task["request_sha256"]:
        raise InvalidRequestError("Host response task or input digest mismatch.")
    actor = value["actor"]
    if not isinstance(actor, Mapping) or set(actor) - {"actor_id", "kind", "context_id"}:
        raise InvalidRequestError("actor must describe actor_id, kind, and optional context_id.")
    if not isinstance(actor.get("actor_id"), str) or not actor["actor_id"].strip():
        raise InvalidRequestError("actor.actor_id is required.")
    if actor.get("kind") not in {"agent", "subagent", "fake"}:
        raise InvalidRequestError("actor.kind must be agent, subagent, or fake.")
    if actor.get("context_id") is not None and (not isinstance(actor["context_id"], str) or not actor["context_id"].strip()):
        raise InvalidRequestError("actor.context_id must be non-empty or null.")
    isolation = value.get("isolation", "unknown")
    if isolation not in {"fresh_context", "inherited", "unknown"}:
        raise InvalidRequestError("Invalid host isolation declaration.")
    binding = task["request"]["binding"]
    if binding.get("fresh_context_required"):
        if actor["kind"] not in {"subagent", "fake"}:
            raise InvalidRequestError("actor.kind must be subagent for this independent worker (fake is for offline fixtures).")
        if isolation != "fresh_context":
            raise InvalidRequestError("isolation must be fresh_context for this independent worker; attest only an actually separate context.")
        if not actor.get("context_id"):
            raise InvalidRequestError("actor.context_id must identify this independent worker's actual context.")
    for name in ("actual_model", "reasoning_effort"):
        if value.get(name) is not None and (not isinstance(value[name], str) or not value[name].strip()):
            raise InvalidRequestError(f"{name} must be non-empty or null.")
    usage = value.get("usage")
    if usage is not None:
        fields = {"input_tokens", "output_tokens", "cached_input_tokens"}
        if not isinstance(usage, Mapping) or set(usage) != fields or any(
            item is not None and (type(item) is not int or item < 0) for item in usage.values()
        ):
            raise InvalidRequestError("usage must contain nullable non-negative token counts.")
    try:
        canonical_json_bytes(value)
    except (ValueError, TypeError) as exc:
        raise InvalidRequestError("Host response must contain finite JSON values.") from exc
    contract = task["request"]["output_contract"]
    if contract["kind"] == "json":
        errors = sorted(Draft202012Validator(contract["schema"]).iter_errors(value["output"]), key=lambda error: str(error.path))
        if errors:
            error = errors[0]
            location = "$.output" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.path)
            raise OutputInvalidError(f"{location}: {error.message}")
    elif not isinstance(value["output"], str) or not value["output"].strip():
        raise OutputInvalidError("$.output must be a non-empty string.")
    result = copy.deepcopy(dict(value))
    result.update({"actual_model": value.get("actual_model"), "reasoning_effort": value.get("reasoning_effort"),
                   "isolation": isolation, "usage": None if usage is None else dict(usage)})
    return result


def prepare_host_task(context: RunContext, request: Any, state: Any, workspace: Path, options: Any,
                      *, provider_prompt: str, recovery: Any = None,
                      session_history: Any = None, host_history: Any = None) -> _Receipt:
    """Persist complete materials before exposing a recoverable handoff."""
    control = strict_json((workspace / "host/control.json").read_bytes())
    inputs = []
    for item in control["inputs"]:
        content = (workspace / item["path"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != item["sha256"] or len(content) != item["size_bytes"]:
            raise InvalidRequestError(f"Host input digest mismatch: {item['input_id']}")
        inputs.append({**item, "content_base64": base64.b64encode(content).decode("ascii")})
    continuation = None
    continuation_files = []
    if control["continuation_response"] is not None:
        continuation = strict_json((workspace / control["continuation_response"]).read_bytes())
        for relative in continuation.get("response", {}).get("files", []):
            content = (workspace / relative).read_bytes()
            continuation_files.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest(),
                                       "content_base64": base64.b64encode(content).decode("ascii")})
    runtime = {key: value for key, value in control["runtime"].items() if key != "ac_environment"}
    material = {
        "provider_prompt": provider_prompt,
        "task_prompt": control["prompt"], "output_contract": control["output_contract"],
        "provider_instructions": control["provider_instructions"], "capabilities": runtime,
        "inputs": inputs, "continuation": continuation, "continuation_files": continuation_files,
        "model_requirement": {"provider": request.model.provider, "model": request.model.model,
                              "tier": request.model.tier, "reasoning_effort": request.model.reasoning_effort,
                              "unavailable_model_policy": "use_host_model"},
        "binding": dict(options.task_binding),
        "accepted_prefix_sha256": None if request.session is None else request.session.accepted_prefix_sha256,
        "recovery": recovery,
        "session_history": session_history or [], "host_history": host_history or [],
    }
    task = {
        "schema_version": TASK_PROTOCOL, "run_id": context.run_id, "llm_task_id": request.task_id,
        "semantic_key_sha256": state.semantic_key.sha256, "generation": state.current.generation,
        "host_turn_round": state.host_turn_round, "recovery_epoch": context.recovery_epoch,
        "request_sha256": _digest(material), "request": material,
        "coordinator": None if options.host_coordinator is None else options.host_coordinator.to_document(),
        "response_contract": RESPONSE_PROTOCOL,
        "route": {"requested_provider": request.model.provider, "final_provider": "host",
                  "fallback_reason": "provider_unavailable_before_start" if request.model.provider not in {"host", "auto"} or request.model.provider == "auto" and options.host_coordinator is not None and options.host_coordinator.default_provider != "host" else None},
        "resume": {"operation": "resume_owning_workflow", "run_id": context.run_id},
    }
    task["task_id"] = _identity(task)
    task["response_schema"] = {
        "type": "object",
        "properties": {
            "schema_version": {"const": RESPONSE_PROTOCOL}, "task_id": {"const": task["task_id"]},
            "request_sha256": {"const": task["request_sha256"]},
            "actor": {"type": "object", "properties": {"actor_id": {"type": "string", "minLength": 1},
                       "kind": {"enum": ["agent", "subagent", "fake"]}, "context_id": {"type": ["string", "null"]}},
                      "required": ["actor_id", "kind"], "additionalProperties": False},
            "output": control["output_contract"].get("schema", {"type": "string", "minLength": 1}),
            "actual_model": {"type": ["string", "null"]}, "reasoning_effort": {"type": ["string", "null"]},
            "isolation": {"enum": ["fresh_context", "inherited", "unknown"]},
            "usage": {"anyOf": [{"type": "null"}, {"type": "object", "properties": {
                name: {"type": ["integer", "null"], "minimum": 0} for name in ("input_tokens", "output_tokens", "cached_input_tokens")},
                "required": ["input_tokens", "output_tokens", "cached_input_tokens"], "additionalProperties": False}]},
        },
        "required": ["schema_version", "task_id", "request_sha256", "actor", "output"], "additionalProperties": False,
    }
    store = _store(context, task["task_id"])
    existing = store.read()
    if existing is not None:
        if existing.task != task:
            raise IdempotencyConflictError("The host task was exported with different materials.")
        return existing
    try:
        return store.create(_Receipt(0, task))
    except StateConflictError:
        existing = store.read()
        if existing is None or existing.task != task:
            raise IdempotencyConflictError("Concurrent host export changed the materials.")
        return existing


def execution_from_receipt(receipt: _Receipt) -> ProviderExecution:
    if receipt.response is None:
        raise InvalidRequestError("No host response has been submitted.")
    response = _response(receipt.task, receipt.response)
    usage = None if response["usage"] is None else ProviderUsage(**response["usage"])
    preference = receipt.task["request"]["model_requirement"]["model"]
    return ProviderExecution(
        ProviderTerminalKind.COMPLETED,
        (CandidateMaterial(value=response["output"], terminal=True),), usage=usage,
        diagnostics={"host_task_id": receipt.task["task_id"], "actor": response["actor"],
                     "actual_model": response["actual_model"], "reasoning_effort": response["reasoning_effort"],
                     "isolation": response["isolation"], "requested_model": preference,
                     "model_selection": "unknown" if response["actual_model"] is None else "host_default" if preference is None else "matched" if preference == response["actual_model"] else "host_model_substitution",
                     "route": receipt.task["route"]},
    )


class HostTaskService:
    """Public operations for standalone and nested workflow host tasks."""

    @staticmethod
    def _context(run_root: str | Path, run_id: str) -> RunContext:
        repository = RunRepository(run_root)
        view = repository.inspect(run_id)
        return RunContext(repository, view.snapshot, resume_input=None)

    def export(self, *, run_root: str | Path, run_id: str, task_id: str) -> dict[str, Any]:
        context = self._context(run_root, run_id)
        receipt = _store(context, task_id).read()
        if receipt is None:
            raise InvalidRequestError("Unknown host task_id in the current recovery epoch.")
        task = copy.deepcopy(dict(receipt.task))
        # Presentation constraints can improve without rewriting durable receipts
        # or changing the identity of an already-paused task.
        if task["request"]["binding"].get("fresh_context_required"):
            schema = task["response_schema"]
            actor = schema["properties"]["actor"]
            actor["properties"]["kind"] = {"enum": ["subagent", "fake"]}
            actor["properties"]["context_id"] = {"type": "string", "pattern": r"\S"}
            actor["required"] = ["actor_id", "kind", "context_id"]
            schema["properties"]["isolation"] = {"const": "fresh_context"}
            if "isolation" not in schema["required"]:
                schema["required"].append("isolation")
        return task

    def materialize(self, *, run_root: str | Path, run_id: str, task_id: str,
                    directory: str | Path) -> dict[str, Any]:
        """Export a verified, self-contained workspace without exposing run internals."""
        task = self.export(run_root=run_root, run_id=run_id, task_id=task_id)
        root = Path(directory).expanduser().resolve()
        work = root / "work"
        if work.is_symlink() or work.exists() and not work.is_dir():
            raise InvalidRequestError("The export work path must be a real directory.")
        files: dict[str, bytes] = {}

        def add(relative: str, content: bytes) -> None:
            if not isinstance(relative, str) or "\\" in relative or any(part in {"", ".", ".."} for part in relative.split("/")):
                raise InvalidRequestError("Export paths must be contained relative POSIX paths.")
            try:
                (root / relative).resolve().relative_to(root)
            except ValueError as exc:
                raise InvalidRequestError("Export path escapes its directory.") from exc
            if relative.casefold() in {path.casefold() for path in files}:
                raise InvalidRequestError("Export paths collide.")
            files[relative] = content

        def unpack(items: list[Mapping[str, Any]], prefix: str = "") -> None:
            for item in items:
                try:
                    content = base64.b64decode(item["content_base64"], validate=True)
                except (ValueError, TypeError) as exc:
                    raise CorruptStateError("Invalid exported input encoding.") from exc
                if hashlib.sha256(content).hexdigest() != item["sha256"]:
                    raise CorruptStateError("Exported input digest mismatch.")
                add(prefix + item["path"], content)

        material = task["request"]
        unpack(material["inputs"])
        unpack(material["continuation_files"])
        for ordinal, item in enumerate(material["session_history"]):
            unpack(item["inputs"], f"history/session/{ordinal}/")
        for item in material["host_history"]:
            unpack(item.get("files", []), f"history/host/{item['round']}/")
        continuation = material["continuation"]
        if continuation is not None:
            add("host/continuation.json", canonical_json_bytes(continuation))
        control = {"schema_version": "ac.llm.workspace_control.v1", "task_id": task["llm_task_id"],
                   "prompt": material["task_prompt"], "output_contract": material["output_contract"],
                   "runtime": material["capabilities"], "inputs": [{key: value for key, value in item.items() if key != "content_base64"} for item in material["inputs"]],
                   "work_directory": "work", "continuation_response": None if continuation is None else "host/continuation.json",
                   "provider_instructions": material["provider_instructions"],
                   "session_history": material["session_history"], "host_history": material["host_history"]}
        add("host/control.json", canonical_json_bytes(control))
        add("task.json", canonical_json_bytes(task))
        fresh = bool(material["binding"].get("fresh_context_required"))
        add("response.template.json", canonical_json_bytes({"schema_version": RESPONSE_PROTOCOL, "task_id": task_id,
            "request_sha256": task["request_sha256"],
            "actor": {"actor_id": "" if fresh else "host", "kind": "subagent" if fresh else "agent", "context_id": None},
            "output": None, "actual_model": None, "reasoning_effort": None, "isolation": "unknown", "usage": None}))
        root.mkdir(parents=True, exist_ok=True)
        for relative, content in files.items():
            destination = root / relative
            if destination.exists() and destination.read_bytes() != content:
                raise IdempotencyConflictError(f"Export would overwrite different content: {relative}; use a new empty export directory and preserve existing worker responses.")
        for relative, content in files.items():
            atomic_write_bytes(root / relative, content)
        work.mkdir(exist_ok=True)
        return {"task_id": task_id, "directory": str(root), "files": sorted(files)}

    def pending(self, *, run_root: str | Path, run_id: str, include_completed: bool = False) -> list[dict[str, Any]]:
        context = self._context(run_root, run_id)
        records = []
        directory = context.run_directory / "host-tasks" / f"epoch-{context.recovery_epoch}"
        for path in sorted(directory.glob("host-*.json")):
            receipt = _store(context, path.stem).read()
            if receipt is None:
                continue
            state = _task_state(context, receipt.task)
            status = "awaiting_host" if receipt.response is None else "submitted"
            if state is None:
                raise CorruptStateError("Host task has no owning LLM state.")
            if state.accepted is not None:
                status = "completed"
            elif state.current.generation != receipt.task["generation"] or state.host_turn_round != receipt.task["host_turn_round"]:
                status = "superseded"
            elif state.current.raw_response is not None:
                status = "consumed"
            elif receipt.response is not None and state.pause is not None and state.pause.details.get("host_task_id") not in {None, receipt.task["task_id"]}:
                status = "consumed"
            stop = context.stop.read()
            if stop is not None and status in {"awaiting_host", "submitted"}:
                status = "stopped"
            if include_completed or status in {"awaiting_host", "submitted", "stopped"}:
                records.append({"task_id": receipt.task["task_id"], "llm_task_id": receipt.task["llm_task_id"],
                                "status": status, "generation": receipt.task["generation"],
                                "host_turn_round": receipt.task["host_turn_round"], "binding": receipt.task["request"]["binding"],
                                "request_sha256": receipt.task["request_sha256"], "response_contract": RESPONSE_PROTOCOL,
                                "actor": None if receipt.response is None else receipt.response["actor"],
                                "actual_model": None if receipt.response is None else receipt.response["actual_model"],
                                "requested_model": receipt.task["request"]["model_requirement"]["model"],
                                "route": receipt.task["route"]})
        return records

    def submit(self, *, run_root: str | Path, run_id: str, response: Mapping[str, Any] | str | bytes) -> dict[str, Any]:
        value = strict_json(response) if isinstance(response, (str, bytes)) else response
        if not isinstance(value, Mapping) or not isinstance(value.get("task_id"), str):
            raise InvalidRequestError("Host submission requires a task_id.")
        context = self._context(run_root, run_id)
        with FileLease(context.run_directory / "host-tasks" / "submit.lock").acquire(blocking=True):
            return self._submit(context, value)

    def _submit(self, context: RunContext, value: Mapping[str, Any]) -> dict[str, Any]:
        run_root, run_id = context.repository.root, context.run_id
        store = _store(context, value["task_id"])
        while True:
            receipt = store.read()
            if receipt is None:
                raise InvalidRequestError("Unknown host task_id.")
            normalized = _response(receipt.task, value)
            if receipt.response is not None:
                if normalized != receipt.response:
                    raise IdempotencyConflictError("A different response is already submitted for this host task.")
                return {"task_id": value["task_id"], "status": "submitted", "reused": True}
            context.checkpoint()
            if context.repository.inspect(run_id).snapshot.status in {RunStatus.FAILED, RunStatus.SUCCEEDED}:
                raise InvalidRequestError("Cannot submit new work to a terminal run.")
            _active(context, receipt.task)
            binding = receipt.task["request"]["binding"]
            if binding.get("fresh_context_required"):
                for peer in self.pending(run_root=run_root, run_id=run_id, include_completed=True):
                    if peer["task_id"] == value["task_id"] or (
                        peer["binding"].get("loop_id"), peer["binding"].get("execution_scope")
                    ) != (binding.get("loop_id"), binding.get("execution_scope")) or (
                        peer["binding"].get("role"), peer["binding"].get("worker_id")
                    ) == (binding.get("role"), binding.get("worker_id")):
                        continue
                    prior = _store(context, peer["task_id"]).read()
                    if prior is not None and prior.response is not None and prior.task["request"]["binding"].get("fresh_context_required") and prior.response["actor"].get("context_id") == normalized["actor"].get("context_id"):
                        raise InvalidRequestError("Independent workers cannot reuse the same host context_id.")
            try:
                store.compare_and_swap(receipt.revision, replace(receipt, revision=receipt.revision + 1, response=normalized))
                return {"task_id": value["task_id"], "status": "submitted", "reused": False}
            except RevisionConflictError:
                continue
