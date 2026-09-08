"""Explicit optional API configuration; persisted documents contain secret references only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .providers.http_api import HTTPAPIAdapter, HTTPProviderConfig


def register_configured_providers(registry: Any, path: str | Path) -> None:
    config_path = Path(path)
    if config_path.stat().st_size > 256 * 1024:
        raise ValueError("Provider configuration exceeds 256 KiB.")
    value = json.loads(config_path.read_text(encoding="utf-8"))
    if (
        set(value) != {"schema_version", "providers"}
        or value["schema_version"] != "ac.llm.providers.v1"
        or not isinstance(value["providers"], list)
    ):
        raise ValueError("Invalid provider configuration.")
    for entry in value["providers"]:
        required = {"name", "protocol", "base_url", "credential"}
        optional = {"max_output_tokens", "reasoning_efforts", "vision"}
        if (
            not isinstance(entry, dict)
            or not required <= set(entry)
            or set(entry) - required - optional
        ):
            raise ValueError(
                "Provider configuration contains missing or unknown fields."
            )
        secret_ref = entry["credential"]
        if (
            not isinstance(secret_ref, dict)
            or set(secret_ref) != {"kind", "name"}
            or secret_ref["kind"] != "environment"
            or not isinstance(secret_ref["name"], str)
            or not secret_ref["name"].isidentifier()
        ):
            raise ValueError("Credentials must be an environment-variable reference.")
        config = HTTPProviderConfig(
            **{k: v for k, v in entry.items() if k != "credential"}
        )
        registry.register(
            config.name,
            lambda c=config, name=secret_ref["name"]: HTTPAPIAdapter(
                c, credential=lambda: os.environ.get(name)
            ),
        )
