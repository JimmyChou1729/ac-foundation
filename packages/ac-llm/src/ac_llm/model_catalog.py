"""Bounded, read-only model discovery for supported local provider CLIs."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


@dataclass(frozen=True)
class ProviderModel:
    model: str
    display_name: str
    description: str
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None
    provider_default: bool

    def to_document(self) -> dict[str, object]:
        return {
            "id": self.model,
            "name": self.display_name,
            "description": self.description,
            "reasoning_efforts": list(self.reasoning_efforts),
            "default_reasoning_effort": self.default_reasoning_effort,
            "provider_default": self.provider_default,
        }


@dataclass(frozen=True)
class ProviderModelCatalog:
    provider: str
    status: str
    source: str
    models: tuple[ProviderModel, ...] = ()
    message: str | None = None


def codex_model_catalog(
    binary: str = "codex", *, timeout_seconds: float = 5.0
) -> ProviderModelCatalog:
    """Read the visible model picker catalog from the local Codex app server."""

    messages = (
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "ac-llm-model-catalog",
                    "title": "AC model catalog",
                    "version": "1",
                }
            },
        },
        {"method": "initialized"},
        {
            "method": "model/list",
            "id": 2,
            "params": {"limit": 100, "includeHidden": False},
        },
    )
    process: subprocess.Popen[str] | None = None
    output: queue.Queue[str | None] = queue.Queue()
    try:
        process = subprocess.Popen(
            [
                binary,
                "app-server",
                "--stdio",
                "-c",
                'model_provider="openai"',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        if process.stdin is None or process.stdout is None:
            raise OSError("Codex app server pipes are unavailable")

        def read_lines() -> None:
            assert process is not None and process.stdout is not None
            try:
                for line in process.stdout:
                    output.put(line)
            finally:
                output.put(None)

        threading.Thread(target=read_lines, daemon=True).start()
        for message in messages:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex model discovery timed out")
            line = output.get(timeout=remaining)
            if line is None:
                raise ValueError("Codex app server exited before model discovery")
            message = json.loads(line)
            if isinstance(message, Mapping) and message.get("id") == 2:
                return ProviderModelCatalog(
                    "codex", "available", "codex_cli", _decode_models(message)
                )
    except (
        OSError,
        ValueError,
        TypeError,
        TimeoutError,
        queue.Empty,
        subprocess.SubprocessError,
    ):
        return ProviderModelCatalog(
            "codex",
            "unavailable",
            "codex_cli",
            message="无法从本机 Codex CLI 读取可用模型。",
        )
    finally:
        if process is not None:
            _terminate(process)


def _decode_models(response: Mapping[str, Any]) -> tuple[ProviderModel, ...]:
    result = response.get("result")
    values = result.get("data") if isinstance(result, Mapping) else None
    if not isinstance(values, list) or not values or len(values) > 100:
        raise ValueError("Codex model catalog has invalid data")
    models: list[ProviderModel] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, Mapping) or raw.get("hidden") is not False:
            continue
        model = str(raw.get("model", ""))
        if not _MODEL_ID.fullmatch(model) or model in seen:
            raise ValueError("Codex model catalog has an invalid model ID")
        seen.add(model)
        options = raw.get("supportedReasoningEfforts")
        if not isinstance(options, list) or len(options) > len(_EFFORTS):
            raise ValueError("Codex model catalog has invalid reasoning efforts")
        efforts = tuple(
            str(item.get("reasoningEffort", ""))
            for item in options
            if isinstance(item, Mapping)
        )
        if len(efforts) != len(options) or len(set(efforts)) != len(efforts):
            raise ValueError("Codex model catalog has invalid reasoning efforts")
        if any(effort not in _EFFORTS for effort in efforts):
            raise ValueError("Codex model catalog has an unsupported reasoning effort")
        default = str(raw.get("defaultReasoningEffort", "")) or None
        if default is not None and default not in efforts:
            raise ValueError("Codex model catalog has an invalid default effort")
        name = str(raw.get("displayName", "")).strip()
        description = str(raw.get("description", "")).strip()
        if not name or len(name) > 120 or len(description) > 500:
            raise ValueError("Codex model catalog has invalid presentation data")
        models.append(
            ProviderModel(
                model,
                name,
                description,
                efforts,
                default,
                raw.get("isDefault") is True,
            )
        )
    if not models:
        raise ValueError("Codex model catalog has no visible models")
    return tuple(models)


def _terminate(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    except OSError:
        pass
