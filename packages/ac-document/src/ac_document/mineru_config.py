"""Explicit project-scoped MinerU configuration, without credential values."""

from pathlib import Path
import json
import re
from ac_jobs import atomic_write_bytes
from .mineru_runner import _url, doctor_mineru, parse_pdf_mineru
from .pdf_source import PDFSourceBundleError, json_bytes, read_bounded


def validate_mineru_config(value):
    if not isinstance(value, dict) or set(value) != {
        "executable",
        "api_url",
        "token_env",
        "language",
    }:
        raise PDFSourceBundleError(
            "mineru_config", "Invalid MinerU configuration fields."
        )
    if any(v is not None and not isinstance(v, str) for v in value.values()):
        raise PDFSourceBundleError(
            "mineru_config", "MinerU configuration values must be strings."
        )
    executable, url, token = value["executable"], value["api_url"], value["token_env"]
    if bool(executable) == bool(url) or value["language"] not in {"en", "ch"}:
        raise PDFSourceBundleError(
            "mineru_config",
            "Choose one executable or service URL and OCR language en or ch.",
        )
    if token is not None and (
        not url or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token)
    ):
        raise PDFSourceBundleError(
            "mineru_config", "Use a token environment variable name with service mode."
        )
    if executable and (not executable.strip() or any(ord(c) < 32 for c in executable)):
        raise PDFSourceBundleError("mineru_config", "Invalid executable path.")
    return {**value, "api_url": _url(url) if url else None}


def load_mineru_config(config_path):
    path = Path(config_path)
    if not path.exists():
        return None
    try:
        value = json.loads(read_bounded(path, 16384))
    except (ValueError, OSError):
        raise PDFSourceBundleError(
            "mineru_config", "MinerU configuration cannot be read."
        ) from None
    return validate_mineru_config(value)


def configure_mineru(
    *, config_path, executable=None, api_url=None, token_env=None, language="en"
):
    value = validate_mineru_config(
        dict(
            executable=executable,
            api_url=api_url,
            token_env=token_env,
            language=language,
        )
    )
    atomic_write_bytes(Path(config_path), json_bytes(value))
    return value


def doctor_configured_mineru(*, config_path):
    value = load_mineru_config(config_path)
    if value is None:
        raise PDFSourceBundleError(
            "mineru_config", "Configure MinerU before using this profile."
        )
    return doctor_mineru(**{k: v for k, v in value.items() if k != "language"})


def parse_pdf_configured_mineru(pdf, *, config_path, job_dir, timeout_seconds=900):
    value = load_mineru_config(config_path)
    if value is None:
        raise PDFSourceBundleError(
            "mineru_config", "Configure MinerU before using this profile."
        )
    return parse_pdf_mineru(
        pdf, job_dir=job_dir, timeout_seconds=timeout_seconds, **value
    )
