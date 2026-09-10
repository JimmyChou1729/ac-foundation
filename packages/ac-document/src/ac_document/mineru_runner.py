"""Bounded MinerU execution with explicit local or connected authority."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import threading
from urllib.parse import urlsplit
import zipfile

import httpx
from ac_jobs import atomic_write_bytes, file_lease

from .mineru import import_mineru_bundle
from .pdf_source import (
    MAX_PDF_BYTES,
    PDFSourceBundleError,
    json_bytes,
    read_bounded,
    safe_relative,
    verify_pdf_source_bundle,
)

VERSION = "3.4.5"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
JOB_SCHEMA = "ac.document.mineru_job.v1"


def _fail(code, message):
    raise PDFSourceBundleError(code, message)


def _url(value):
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        _fail("mineru_config", "Invalid MinerU service URL.")
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "\\" in value
        or any(ord(c) < 33 for c in value)
    ):
        _fail(
            "mineru_config",
            "Use an HTTP(S) service URL without credentials or query parameters.",
        )
    if parts.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = parts.hostname == "localhost"
        if not loopback:
            _fail(
                "mineru_config",
                "Remote MinerU connections require HTTPS; HTTP is limited to loopback.",
            )
    if parts.path not in {"", "/"}:
        safe_relative(parts.path.strip("/"))
    return value.rstrip("/")


def _config(executable, api_url, token_env, language):
    if bool(executable) == bool(api_url):
        _fail("mineru_config", "Choose exactly one MinerU executable or service URL.")
    if language not in {"en", "ch"}:
        _fail("mineru_config", "Supported OCR languages are en and ch.")
    if token_env is not None and (
        not api_url or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env)
    ):
        _fail("mineru_config", "A token environment reference requires service mode.")
    if executable:
        if os.name != "posix":
            _fail(
                "mineru_platform",
                "Local execution currently requires macOS or Linux; use service mode on other platforms.",
            )
        resolved = shutil.which(str(executable))
        if resolved is None:
            _fail(
                "mineru_not_installed",
                "MinerU executable was not found; install the optional runtime separately.",
            )
        executable = str(Path(resolved).absolute())
    return dict(
        mode="service" if api_url else "local",
        executable=executable,
        api_url=_url(api_url) if api_url else None,
        token_env=token_env,
        language=language,
        version=VERSION,
        backend="pipeline",
        parse_method="auto",
    )


def _headers(config):
    if not config["token_env"]:
        return {}
    token = os.environ.get(config["token_env"], "")
    if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        _fail(
            "mineru_credentials",
            "The configured token environment variable is missing or invalid.",
        )
    return {"Authorization": "Bearer " + token}


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        _fail(
            "mineru_timeout",
            "MinerU deadline reached; resume the same job to query saved service work.",
        )
    return value


def _request(
    client, config, method, path, deadline, *, target=None, checkpoint=None, **kwargs
):
    limit = MAX_ARCHIVE_BYTES if target else MAX_RESPONSE_BYTES
    try:
        with client.stream(
            method,
            config["api_url"] + path,
            timeout=min(30, _remaining(deadline)),
            **kwargs,
        ) as response:
            if response.status_code == 404:
                _fail(
                    "mineru_task_lost",
                    "MinerU task is missing or expired; it was not resubmitted.",
                )
            expected = 202 if method == "POST" else 200
            if response.status_code != expected:
                _fail(
                    "mineru_http",
                    f"MinerU returned HTTP {response.status_code}; no redirect or automatic resubmission was attempted.",
                )
            payload = bytearray()
            total = 0
            handle = target.open("xb") if target else None
            try:
                for chunk in response.iter_bytes():
                    _remaining(deadline)
                    if checkpoint is not None and method == "GET":
                        checkpoint()
                    total += len(chunk)
                    if total > limit:
                        _fail(
                            "mineru_response_limit",
                            "MinerU response exceeds the size limit.",
                        )
                    if handle:
                        handle.write(chunk)
                    else:
                        payload.extend(chunk)
            finally:
                if handle:
                    handle.close()
        if target:
            return None
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except PDFSourceBundleError:
        raise
    except (httpx.HTTPError, OSError):
        _fail(
            "mineru_transport",
            "MinerU connection failed; saved work can be resumed without automatic resubmission.",
        )
    except (ValueError, UnicodeError):
        _fail("mineru_protocol", "MinerU returned invalid JSON.")


def _health(client, config, deadline):
    value = _request(client, config, "GET", "/health", deadline)
    if (
        value.get("status") != "healthy"
        or value.get("version") != VERSION
        or type(value.get("protocol_version")) is not int
        or value["protocol_version"] != 2
    ):
        _fail(
            "mineru_version", "A healthy MinerU 3.4.5 / protocol 2 service is required."
        )


def _stop(process):
    if os.name != "posix":  # pragma: no cover
        if process.poll() is None:
            process.kill()
        process.wait()
        return
    # The group may outlive its leader. Always clean residual owned children.
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _version(executable, deadline=None):
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            timeout=min(15, _remaining(deadline)) if deadline is not None else 15,
        )
    except subprocess.TimeoutExpired:
        _fail("mineru_timeout", "MinerU version check timed out; OCR has not started.")
    except OSError:
        _fail("mineru_not_installed", "MinerU version check failed.")
    if result.returncode or not re.search(rb"\bversion 3\.4\.5\s*$", result.stdout):
        _fail("mineru_version", "The supported local runtime is MinerU 3.4.5.")


def doctor_mineru(*, executable=None, api_url=None, token_env=None):
    config = _config(executable, api_url, token_env, "en")
    if config["mode"] == "local":
        _version(config["executable"])
    else:
        with httpx.Client(
            headers=_headers(config), follow_redirects=False, trust_env=False
        ) as client:
            _health(client, config, time.monotonic() + 15)
    return {
        "available": True,
        "mode": config["mode"],
        "version": VERSION,
        "backend": "pipeline",
        "inference_verified": False,
        "warnings": [
            "Availability does not verify model files or inference; run a small PDF to check them."
        ],
    }


def _extract(archive, destination, checkpoint=None):
    checkpoint = checkpoint or (lambda: None)
    checkpoint()
    try:
        with zipfile.ZipFile(archive) as z:
            members = z.infolist()
            if (
                len(members) > 10000
                or sum(m.file_size for m in members) > MAX_ARCHIVE_BYTES
            ):
                _fail("mineru_archive", "MinerU archive exceeds extraction limits.")
            seen = set()
            for member in members:
                relative = safe_relative(member.filename.rstrip("/"))
                mode = member.external_attr >> 16
                if (
                    relative in seen
                    or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
                    or member.flag_bits & 1
                ):
                    _fail(
                        "mineru_archive",
                        "MinerU archive contains duplicate, encrypted or non-regular entries.",
                    )
                seen.add(relative)
            destination.mkdir()
            total = 0
            for member in members:
                checkpoint()
                path = destination / member.filename
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, path.open("xb") as dst:
                    while chunk := src.read(65536):
                        checkpoint()
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            _fail(
                                "mineru_archive",
                                "MinerU archive exceeds extraction limits.",
                            )
                        dst.write(chunk)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        _fail("mineru_archive", "MinerU result archive is invalid.")


def _save(root, state):
    atomic_write_bytes(root / "job.json", json_bytes(state))


def _finish(root, state, checkpoint=None):
    checkpoint = checkpoint or (lambda: None)
    checkpoint()
    bundle = root / "bundle"
    if bundle.exists():
        manifest = verify_pdf_source_bundle(bundle / "manifest.json")
        if manifest["original"]["sha256"] != state["source_sha256"]:
            _fail("mineru_job_corrupt", "Saved bundle belongs to another input.")
    else:
        lists = list((root / "raw").rglob("*_content_list.json"))
        middles = list((root / "raw").rglob("*_middle.json"))
        if len(lists) != 1 or len(middles) != 1 or lists[0].parent != middles[0].parent:
            _fail(
                "mineru_result",
                "MinerU result must contain one content list and one middle file together.",
            )
        import_mineru_bundle(
            root / "input.pdf",
            content_list=lists[0],
            middle_json=middles[0],
            output_dir=bundle,
        )
    checkpoint()
    state["status"] = "completed"
    _save(root, state)
    return {
        "status": "completed",
        "job_dir": str(root),
        "manifest": str(bundle / "manifest.json"),
        "source": str(bundle / "source.html"),
        "warnings": verify_pdf_source_bundle(bundle / "manifest.json")["warnings"],
    }


def parse_pdf_mineru(
    pdf,
    *,
    job_dir,
    executable=None,
    api_url=None,
    token_env=None,
    language="en",
    timeout_seconds=900,
    checkpoint=None,
):
    """Run or resume one input-bound job. Uncertain submissions are never repeated."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 86400
    ):
        _fail("mineru_config", "Timeout must be between 0 and 86400 seconds.")
    checkpoint = checkpoint or (lambda: None)
    checkpoint()
    config = _config(executable, api_url, token_env, language)
    payload = read_bounded(Path(pdf), MAX_PDF_BYTES)
    if not payload.startswith(b"%PDF-"):
        _fail("mineru_input", "Input is not a PDF.")
    digest = hashlib.sha256(payload).hexdigest()
    root = Path(job_dir).absolute()
    # Job directories are private application-owned state, never provider paths.
    if root.is_symlink():
        _fail("mineru_job_corrupt", "Job directory must not be a symlink.")
    root.parent.mkdir(parents=True, exist_ok=True)
    with file_lease(root.parent / ("." + root.name + ".mineru.lock")):
        if any(
            (root / name).is_symlink()
            for name in ("job.json", "input.pdf", "raw", "bundle", "local-ocr.log", "local-ocr.json")
        ):
            _fail("mineru_job_corrupt", "Saved job paths must not be symlinks.")
        if not root.exists():
            stage = Path(tempfile.mkdtemp(prefix=".mineru-", dir=root.parent))
            try:
                state = dict(
                    schema_version=JOB_SCHEMA,
                    config=config,
                    source_sha256=digest,
                    status="prepared",
                    task_id=None,
                )
                atomic_write_bytes(stage / "input.pdf", payload)
                _save(stage, state)
                stage.rename(root)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        try:
            state = json.loads(read_bounded(root / "job.json", MAX_RESPONSE_BYTES))
            if (
                not isinstance(state, dict)
                or set(state)
                != {"schema_version", "config", "source_sha256", "status", "task_id"}
                or state["schema_version"] != JOB_SCHEMA
                or state["config"] != config
                or state["source_sha256"] != digest
                or hashlib.sha256(
                    read_bounded(root / "input.pdf", MAX_PDF_BYTES)
                ).hexdigest()
                != digest
            ):
                raise ValueError
            if state["status"] not in {
                "prepared",
                "submitting",
                "submitted",
                "running",
                "ready",
                "completed",
            }:
                raise ValueError
            if state["task_id"] is not None and not re.fullmatch(
                r"[A-Za-z0-9_-]{1,128}", state["task_id"]
            ):
                raise ValueError
        except (ValueError, TypeError, OSError):
            _fail(
                "mineru_job_corrupt",
                "Job state or input differs; use the original configuration and source.",
            )
        if state["status"] in {"ready", "completed"}:
            return _finish(root, state, checkpoint)
        if (
            config["mode"] == "service"
            and state["status"] == "submitted"
            and (root / "raw").exists()
        ):
            try:
                receipt = json.loads(
                    read_bounded(root / "raw" / ".ac-result.json", MAX_RESPONSE_BYTES)
                )
            except (ValueError, OSError):
                _fail(
                    "mineru_job_corrupt", "Saved result has no valid download receipt."
                )
            if receipt != {"task_id": state["task_id"], "source_sha256": digest}:
                _fail(
                    "mineru_job_corrupt",
                    "Saved download receipt differs from this job.",
                )
            state["status"] = "ready"
            _save(root, state)
            return _finish(root, state, checkpoint)
        deadline = time.monotonic() + timeout_seconds
        if config["mode"] == "local":
            if state["status"] != "prepared":
                _fail(
                    "mineru_local_interrupted",
                    "Previous local execution did not finish; inspect it before explicitly starting a new job directory.",
                )
            _version(config["executable"], deadline)
            checkpoint()
            _remaining(deadline)
            state["status"] = "running"
            _save(root, state)
            try:
                process = subprocess.Popen(
                    [
                        config["executable"],
                        "-p",
                        str(root / "input.pdf"),
                        "-o",
                        str(root / "raw"),
                        "-b",
                        "pipeline",
                        "-m",
                        "auto",
                        "-l",
                        language,
                    ],
                    env={
                        **os.environ,
                        "MINERU_LOCAL_API_LAUNCH_MODE": "subprocess",
                        "NO_PROXY": "*",
                        "no_proxy": "*",
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name == "posix",
                )
            except OSError:
                _fail("mineru_local_failed", "MinerU could not start.")
            output = bytearray()
            def drain_output():
                while chunk := process.stdout.read(4096):
                    output.extend(chunk)
                    del output[:-65536]
            reader = threading.Thread(target=drain_output, daemon=True)
            reader.start()
            try:
                while process.poll() is None:
                    checkpoint()
                    try:
                        process.wait(timeout=min(0.25, _remaining(deadline)))
                    except subprocess.TimeoutExpired:
                        continue
                code = process.returncode
            except subprocess.TimeoutExpired:
                _fail(
                    "mineru_timeout",
                    "Local OCR timed out; its owned process group was stopped.",
                )
            finally:
                _stop(process)
                reader.join(timeout=5)
                diagnostic = output.decode("utf-8", errors="replace")
                for name, value in os.environ.items():
                    if value and re.search(r"token|secret|password|api.?key|access.?key", name, re.I):
                        diagnostic = diagnostic.replace(value, "[REDACTED]")
                diagnostic = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", diagnostic)
                diagnostic = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", diagnostic)
                atomic_write_bytes(root / "local-ocr.log", diagnostic.encode("utf-8"))
                (root / "local-ocr.log").chmod(0o600)
                atomic_write_bytes(root / "local-ocr.json", json_bytes({
                    "exit_code": process.returncode, "log": "local-ocr.log",
                }))
                (root / "local-ocr.json").chmod(0o600)
            if code:
                _fail(
                    "mineru_local_failed",
                    f"Local OCR failed (exit code {code}); inspect local-ocr.log in the OCR job directory.",
                )
        else:
            if state["status"] == "submitting":
                _fail(
                    "mineru_submission_uncertain",
                    "The previous submission has no saved task ID; it may still be running. Inspect the service before creating a new job.",
                )
            with httpx.Client(
                headers=_headers(config), follow_redirects=False, trust_env=False
            ) as client:
                _health(client, config, deadline)
                checkpoint()
                if state["status"] == "prepared":
                    state["status"] = "submitting"
                    _save(root, state)
                    data = dict(
                        backend="pipeline",
                        parse_method="auto",
                        lang_list=language,
                        formula_enable="true",
                        table_enable="true",
                        return_md="false",
                        return_middle_json="true",
                        return_content_list="true",
                        return_images="true",
                        return_model_output="false",
                        return_original_file="false",
                        response_format_zip="true",
                    )
                    reply = _request(
                        client,
                        config,
                        "POST",
                        "/tasks",
                        deadline,
                        data=data,
                        files={"files": ("input.pdf", payload, "application/pdf")},
                    )
                    task_id = reply.get("task_id")
                    if not isinstance(task_id, str) or not re.fullmatch(
                        r"[A-Za-z0-9_-]{1,128}", task_id
                    ):
                        _fail(
                            "mineru_protocol",
                            "MinerU returned an invalid task ID; submission was not repeated.",
                        )
                    state.update(task_id=task_id, status="submitted")
                    _save(root, state)
                if state["status"] != "submitted" or not state["task_id"]:
                    _fail("mineru_job_corrupt", "Invalid saved service state.")
                path = "/tasks/" + state["task_id"]
                while True:
                    checkpoint()
                    reply = _request(client, config, "GET", path, deadline)
                    status = reply.get("status")
                    if status == "completed":
                        break
                    if status == "failed":
                        _fail(
                            "mineru_remote_failed",
                            "MinerU task failed; it was not resubmitted.",
                        )
                    if status not in {"pending", "processing"}:
                        _fail(
                            "mineru_protocol", "MinerU returned an unknown task status."
                        )
                    time.sleep(min(1, _remaining(deadline)))
                stage = Path(tempfile.mkdtemp(prefix=".download-", dir=root))
                try:
                    _request(
                        client,
                        config,
                        "GET",
                        path + "/result",
                        deadline,
                        target=stage / "result.zip",
                        checkpoint=checkpoint,
                    )
                    checkpoint()
                    _extract(stage / "result.zip", stage / "raw", checkpoint)
                    if (stage / "raw" / ".ac-result.json").exists():
                        _fail(
                            "mineru_archive",
                            "Archive contains a reserved application filename.",
                        )
                    atomic_write_bytes(
                        stage / "raw" / ".ac-result.json",
                        json_bytes(
                            {"task_id": state["task_id"], "source_sha256": digest}
                        ),
                    )
                    if (root / "raw").exists():
                        _fail(
                            "mineru_job_corrupt",
                            "Uncommitted result directory exists; inspect it before retrying.",
                        )
                    (stage / "raw").rename(root / "raw")
                finally:
                    shutil.rmtree(stage)
        state["status"] = "ready"
        _save(root, state)
        return _finish(root, state, checkpoint)
