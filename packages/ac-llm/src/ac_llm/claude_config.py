"""Connection-only settings for isolated Claude CLI calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .errors import FailureCategory, ProviderFailure

_CONNECTION_KEYS = frozenset({
    "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
})
_UNSUPPORTED_KEYS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def claude_connection_environment(
    environment: Mapping[str, str] | None = None, *, config_path: Path | None = None,
) -> dict[str, str]:
    """Keep credentials in child environment, never argv or durable task inputs."""
    values = dict(os.environ if environment is None else environment)
    path = config_path or Path(values.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "settings.json"
    try:
        settings = {}
        if path.is_file():
            if path.stat().st_size > 256 * 1024:
                raise ValueError()
            settings = json.loads(path.read_text())
        if not isinstance(settings, dict):
            raise ValueError()
        configured = settings.get("env", {})
        if not isinstance(configured, dict):
            raise ValueError()
        if any(settings.get(k) for k in ("apiKeyHelper", "forceLoginMethod", "forceLoginOrgUUID")):
            raise ValueError()
        if any(values.get(k) or configured.get(k) for k in _UNSUPPORTED_KEYS):
            raise ValueError()
        for key in _CONNECTION_KEYS:
            value = configured.get(key)
            if value is not None:
                if not isinstance(value, str) or "\x00" in value:
                    raise ValueError()
                # Explicit process overrides take precedence over user settings.
                values.setdefault(key, value)
        url = values.get("ANTHROPIC_BASE_URL")
        if url:
            parts = urlsplit(url)
            if (parts.scheme not in {"http", "https"} or not parts.hostname
                    or parts.username or parts.password or parts.query or parts.fragment):
                raise ValueError()
    except (OSError, ValueError, TypeError):
        raise ProviderFailure(
            "Claude connection settings are invalid or use unsupported helper/cloud authentication. Configure a service URL and token/API key, or use official CLI login.",
            category=FailureCategory.INVALID_REQUEST, retryable=False,
        ) from None
    return values
