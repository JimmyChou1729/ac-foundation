"""Offline environment checks without credential values or provider launches."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .host_execution import HostCoordinator
from .errors import InvalidRequestError


def environment_diagnostics(
    *, project_dir: str | Path | None = None, env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Describe local prerequisites; network and authentication stay unprobed."""
    values = os.environ if env is None else env
    proxies = []
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        if not values.get(name):
            continue
        try:
            scheme = urlsplit(values[name]).scheme.lower()
            if scheme not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"} or "://" not in values[name]:
                scheme = "unsupported_or_unspecified"
        except ValueError:
            scheme = "invalid"
        proxies.append({"variable": name, "scheme": scheme or "unspecified"})
    socks_required = any(item["scheme"].startswith("socks") for item in proxies)
    socks_installed = importlib.util.find_spec("socksio") is not None
    roots = []
    for name in ("AC_FOUNDATION_REPO_ROOT", "AC_PRODUCT_REPO_ROOT"):
        value = values.get(name)
        roots.append({"variable": name, "configured": bool(value),
                      "packages_readable": bool(value) and (Path(value).expanduser() / "packages").is_dir()})
    try:
        coordinator = HostCoordinator.from_environment(env=values)
        host = {"configured": coordinator is not None,
                "capabilities": None if coordinator is None else coordinator.to_document(),
                "runtime_tools_verified": False}
    except InvalidRequestError as exc:
        host = {"configured": False, "configuration_error": type(exc).__name__, "runtime_tools_verified": False}
    write = {"status": "not_checked", "guidance": "Supply --project-dir to probe a temporary file in an existing project."}
    if project_dir is not None:
        project = Path(project_dir).expanduser()
        try:
            with tempfile.TemporaryFile(dir=project) as handle:
                handle.write(b"ac-doctor")
                handle.flush()
            write = {"status": "available"}
        except OSError as exc:
            write = {"status": "unavailable", "error_type": type(exc).__name__,
                     "guidance": "Use an existing writable project directory."}
    return {
        "schema_version": "ac.llm.environment_doctor.v1",
        "python": {"version": sys.version.split()[0], "supported": sys.version_info >= (3, 11)},
        "source_acquisition": {"git_available": shutil.which("git") is not None, "local_roots": roots,
                               "guidance": "Product local installs require both source roots; use runtime doctor for the selected lock and readiness."},
        "network": {"status": "not_checked", "authentication": "not_checked"},
        "proxy": {"configured": proxies, "no_proxy_set": bool(values.get("NO_PROXY") or values.get("no_proxy")),
                  "socks_required": socks_required, "socksio_installed": socks_installed,
                  "status": "missing_optional_dependency" if socks_required and not socks_installed else "configured" if proxies else "direct",
                  "guidance": "For HTTPX over SOCKS, explicitly install httpx[socks] in the selected environment; doctor never installs dependencies."},
        "host": host,
        "write": write,
    }
