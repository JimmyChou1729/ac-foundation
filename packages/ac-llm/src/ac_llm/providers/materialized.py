"""Verified workspace inputs for providers without filesystem tools."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..errors import FailureCategory, ProviderFailure


def materialized_prompt(workspace: Path) -> tuple[str, tuple[tuple[str, bytes], ...]]:
    root = workspace.resolve()
    control_path = root / "host" / "control.json"
    if control_path.stat().st_size > 12 * 1024 * 1024:
        raise ValueError("Provider control exceeds the input limit.")
    control = json.loads(control_path.read_text(encoding="utf-8"))
    if control.get("schema_version") != "ac.llm.workspace_control.v1":
        raise ValueError("Unsupported provider workspace.")
    sections = [str(control.get("provider_instructions") or ""), str(control["prompt"])]
    if control.get("host_history"):
        sections.append(
            "Previously completed host requests and their verified responses:\n"
            + json.dumps(control["host_history"], ensure_ascii=False)
        )
    images: list[tuple[str, bytes]] = []
    total = len("".join(sections).encode())
    for item in control["inputs"]:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root / "inputs") or not path.is_file():
            raise ValueError("Provider input is outside the verified workspace.")
        if path.stat().st_size > 12 * 1024 * 1024:
            raise ValueError("Provider input exceeds the input limit.")
        payload = path.read_bytes()
        total += len(payload)
        if (
            total > 12 * 1024 * 1024
            or len(payload) != item["size_bytes"]
            or hashlib.sha256(payload).hexdigest() != item["sha256"]
        ):
            raise ValueError("Provider input size or digest is invalid.")
        media = item["media_type"]
        if media in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            images.append((media, payload))
        elif media.startswith("text/") or media in {
            "application/json",
            "application/tex",
            "application/x-tex",
            "application/x-latex",
        }:
            sections.append(
                f"Verified input {item['input_id']} ({media}):\n{payload.decode('utf-8')}"
            )
        else:
            raise ProviderFailure(
                "The selected provider cannot read this input type.",
                category=FailureCategory.INVALID_REQUEST,
                details={"media_type": media},
            )
    continuation = control.get("continuation_response")
    if continuation:
        path = (root / continuation).resolve()
        if not path.is_relative_to(root / "host") or path.stat().st_size > 1024 * 1024:
            raise ValueError("Invalid host continuation.")
        sections.append("Host response:\n" + path.read_text(encoding="utf-8"))
    sections.append(
        "Return only the requested output. Output contract:\n"
        + json.dumps(control["output_contract"], ensure_ascii=False)
    )
    return "\n\n".join(sections), tuple(images)
