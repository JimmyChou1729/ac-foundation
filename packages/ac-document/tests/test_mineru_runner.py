from __future__ import annotations
import io
import json
from pathlib import Path
import sys
import zipfile

import httpx
import pytest

from ac_document import (
    doctor_mineru,
    parse_pdf_mineru,
    PDFSourceBundleError,
    verify_pdf_source_bundle,
)
from ac_document import mineru_runner as runner
from ac_document.parse import PDFTextLayer


@pytest.fixture
def input_pdf(tmp_path, monkeypatch):
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsynthetic\n")

    class Pages:
        def extract(self, payload):
            return PDFTextLayer(("",))

    monkeypatch.setattr("ac_document.mineru.PdftotextExtractor", Pages)
    return pdf


def raw_files():
    return {
        "input/auto/input_content_list.json": json.dumps(
            [{"type": "text", "text": "OCR source text", "page_idx": 0}]
        ).encode(),
        "input/auto/input_middle.json": json.dumps(
            {
                "_backend": "pipeline",
                "_version_name": "3.4.5",
                "pdf_info": [
                    {
                        "page_idx": 0,
                        "page_size": [595, 842],
                        "para_blocks": [
                            {
                                "type": "text",
                                "lines": [{"spans": [{"content": "OCR source text"}]}],
                            }
                        ],
                        "discarded_blocks": [],
                    }
                ],
            }
        ).encode(),
    }


def archive(files=None):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, value in (files or raw_files()).items():
            z.writestr(name, value)
    return b.getvalue()


@pytest.fixture
def server(monkeypatch):
    calls = []
    options = {
        "status": "completed",
        "archive": archive(),
        "health_version": "3.4.5",
        "post_status": 202,
    }

    def handle(request):
        calls.append(request)
        path = request.url.path
        if path == "/health":
            return httpx.Response(
                200,
                json={
                    "status": "healthy",
                    "version": options["health_version"],
                    "protocol_version": 2,
                },
            )
        if path == "/tasks":
            return httpx.Response(
                options["post_status"],
                json={
                    "task_id": "task-1",
                    "result_url": "https://untrusted.invalid/steal",
                },
            )
        if path == "/tasks/task-1":
            if options["status"] == "lost":
                return httpx.Response(404)
            return httpx.Response(200, json={"status": options["status"]})
        if path == "/tasks/task-1/result":
            if options.get("stream") is not None:
                return httpx.Response(200, stream=options["stream"])
            return httpx.Response(200, content=options["archive"])
        raise AssertionError(path)

    real_client = httpx.Client
    monkeypatch.setattr(
        runner.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return calls, options


def run(pdf, **kwargs):
    return parse_pdf_mineru(
        pdf, job_dir=pdf.parent / "job", api_url="https://ocr.example", **kwargs
    )


def test_service_completes_and_reuses_without_network(input_pdf, server):
    calls, _ = server
    result = run(input_pdf)
    m = verify_pdf_source_bundle(result["manifest"])
    assert m["pages"][0]["status"] == "parsed"
    assert "OCR source text" in Path(result["source"]).read_text()
    assert len(calls) == 4
    assert run(input_pdf) == result
    assert len(calls) == 4
    assert all(r.url.host == "ocr.example" for r in calls)
    assert b'filename="input.pdf"' in calls[1].content
    assert b"return_middle_json" in calls[1].content


def test_timeout_resumes_saved_task_without_resubmission(input_pdf, server):
    calls, options = server
    options["status"] = "processing"
    with pytest.raises(PDFSourceBundleError) as e:
        run(input_pdf, timeout_seconds=0.02)
    assert e.value.code == "mineru_timeout"
    assert (
        json.loads((input_pdf.parent / "job/job.json").read_text())["task_id"]
        == "task-1"
    )
    options["status"] = "completed"
    assert run(input_pdf)["status"] == "completed"
    assert sum(r.method == "POST" for r in calls) == 1


@pytest.mark.parametrize("post_status", [302, 500])
def test_submission_uncertainty_is_not_retried(input_pdf, server, post_status):
    calls, options = server
    options["post_status"] = post_status
    with pytest.raises(PDFSourceBundleError):
        run(input_pdf)
    before = len(calls)
    with pytest.raises(PDFSourceBundleError) as e:
        run(input_pdf)
    assert e.value.code == "mineru_submission_uncertain"
    assert len(calls) == before


@pytest.mark.parametrize(
    "status,code",
    [
        ("lost", "mineru_task_lost"),
        ("failed", "mineru_remote_failed"),
        ("unknown", "mineru_protocol"),
    ],
)
def test_remote_failures_are_explicit_and_do_not_resubmit(
    input_pdf, server, status, code
):
    calls, options = server
    options["status"] = status
    for _ in range(2):
        with pytest.raises(PDFSourceBundleError) as e:
            run(input_pdf)
        assert e.value.code == code
    assert sum(r.method == "POST" for r in calls) == 1


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "a/../../escape", "a%2fb", "a\\b"]
)
def test_archive_path_escape_is_rejected(input_pdf, server, name):
    _, options = server
    options["archive"] = archive({name: b"bad"})
    with pytest.raises(PDFSourceBundleError):
        run(input_pdf)
    assert not (input_pdf.parent / "job/bundle").exists()
    assert not (input_pdf.parent / "escape").exists()


def test_archive_symlink_rejected(input_pdf, server):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        z.writestr(info, "/private/file")
    server[1]["archive"] = data.getvalue()
    with pytest.raises(PDFSourceBundleError) as e:
        run(input_pdf)
    assert e.value.code == "mineru_archive"


def test_changed_input_or_options_cannot_reuse_job(input_pdf, server):
    run(input_pdf)
    with pytest.raises(PDFSourceBundleError):
        run(input_pdf, language="ch")
    input_pdf.write_bytes(b"%PDF-changed")
    with pytest.raises(PDFSourceBundleError):
        run(input_pdf)


def test_token_reference_not_value_is_persisted(input_pdf, server, monkeypatch):
    monkeypatch.setenv("TEST_OCR_SECRET", "test-secret-value")
    run(input_pdf, token_env="TEST_OCR_SECRET")
    assert all(
        r.headers["Authorization"] == "Bearer test-secret-value" for r in server[0]
    )
    text = (input_pdf.parent / "job/job.json").read_text()
    assert "TEST_OCR_SECRET" in text
    assert "test-secret-value" not in text


@pytest.mark.parametrize(
    "url",
    [
        "http://ocr.example",
        "https://user:secret@ocr.example",
        "https://ocr.example?key=secret",
        "https://ocr.example/#token",
    ],
)
def test_unsafe_service_config_rejected(url):
    with pytest.raises(PDFSourceBundleError):
        doctor_mineru(api_url=url)


def test_doctor_rejects_version_before_upload(input_pdf, server):
    server[1]["health_version"] = "3.5.0"
    with pytest.raises(PDFSourceBundleError) as e:
        run(input_pdf)
    assert e.value.code == "mineru_version"
    assert all(r.method == "GET" for r in server[0])


def fake_executable(tmp_path, *, fail=False):
    p = tmp_path / "mineru"
    p.write_text(
        "#!"
        + sys.executable
        + "\n"
        + f"""
import sys, pathlib
if '--version' in sys.argv:
    print('mineru, version 3.4.5')
    raise SystemExit(0)
if {fail!r}: raise SystemExit(1)
root=pathlib.Path(sys.argv[sys.argv.index('-o')+1])
for name, content in {raw_files()!r}.items():
    path=root/name
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(content)
"""
    )
    p.chmod(0o700)
    return str(p)


def test_local_cli_produces_same_bundle(input_pdf):
    executable = fake_executable(input_pdf.parent)
    assert doctor_mineru(executable=executable)["inference_verified"] is False
    result = parse_pdf_mineru(
        input_pdf, job_dir=input_pdf.parent / "job", executable=executable
    )
    assert (
        verify_pdf_source_bundle(result["manifest"])["pages"][0]["status"] == "parsed"
    )
    assert (
        parse_pdf_mineru(
            input_pdf, job_dir=input_pdf.parent / "job", executable=executable
        )
        == result
    )


def test_local_failure_is_not_automatically_reexecuted(input_pdf):
    executable = fake_executable(input_pdf.parent, fail=True)
    for code in ["mineru_local_failed", "mineru_local_failed"]:
        with pytest.raises(PDFSourceBundleError) as e:
            parse_pdf_mineru(
                input_pdf, job_dir=input_pdf.parent / "job", executable=executable
            )
        assert e.value.code == code


def test_download_publish_crash_recovers_locally(input_pdf, server, monkeypatch):
    save = runner._save

    def interrupted(root, state):
        if state["status"] == "ready":
            raise OSError("simulated crash")
        save(root, state)

    monkeypatch.setattr(runner, "_save", interrupted)
    with pytest.raises(OSError):
        run(input_pdf)
    before = len(server[0])
    monkeypatch.setattr(runner, "_save", save)
    assert run(input_pdf)["status"] == "completed"
    assert len(server[0]) == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_cleanup_kills_children_after_leader_exits(tmp_path):
    import os
    import signal
    import subprocess
    import time

    marker = tmp_path / "ticks"
    child_code = (
        "import signal,time,pathlib; signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); p=pathlib.Path("
        + repr(str(marker))
        + "); exec('while True:\\n p.write_text(str(time.monotonic()))\\n time.sleep(.02)')"
    )
    parent_code = (
        'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'
        + repr(child_code)
        + "]); time.sleep(.2)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent_code], start_new_session=True
    )
    try:
        process.wait(timeout=3)
        assert marker.exists()
        runner._stop(process)
        time.sleep(0.1)
        value = marker.read_text()
        time.sleep(0.1)
        assert marker.read_text() == value
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_version_timeout_keeps_job_prepared(input_pdf):
    executable = input_pdf.parent / "slow-mineru"
    executable.write_text(
        "#!"
        + sys.executable
        + '\nimport time; time.sleep(1); print("mineru, version 3.4.5")\n'
    )
    executable.chmod(0o700)
    with pytest.raises(PDFSourceBundleError) as e:
        parse_pdf_mineru(
            input_pdf,
            job_dir=input_pdf.parent / "job",
            executable=str(executable),
            timeout_seconds=0.03,
        )
    assert e.value.code == "mineru_timeout"
    assert (
        json.loads((input_pdf.parent / "job/job.json").read_text())["status"]
        == "prepared"
    )


def test_cli_doctor_and_parse(input_pdf, server, capsys):
    from ac_document.cli import main

    assert main(["doctor-mineru", "--api-url", "https://ocr.example"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["available"] is True
    assert (
        main(
            [
                "parse-pdf-mineru",
                str(input_pdf),
                "--job-dir",
                str(input_pdf.parent / "job"),
                "--api-url",
                "https://ocr.example",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    assert Path(result["data"]["manifest"]).is_file()


def test_checkpoint_stops_local_execution(input_pdf):
    from ac_jobs import StoppedError

    executable = input_pdf.parent / "cancellable-mineru"
    executable.write_text(
        "#!"
        + sys.executable
        + '\nimport sys,time\nif "--version" in sys.argv: print("mineru, version 3.4.5")\nelse: time.sleep(30)\n'
    )
    executable.chmod(0o700)
    calls = []

    def checkpoint():
        calls.append(True)
        if len(calls) > 2:
            raise StoppedError("Requested pause")

    with pytest.raises(StoppedError):
        parse_pdf_mineru(
            input_pdf,
            job_dir=input_pdf.parent / "job",
            executable=str(executable),
            checkpoint=checkpoint,
        )
    assert len(calls) == 3


def test_configured_cli_shares_profile_with_python(input_pdf, server, capsys):
    from ac_document import (
        configure_mineru,
        load_mineru_config,
        parse_pdf_configured_mineru,
    )
    from ac_document.cli import main

    path = input_pdf.parent / ".ac/mineru.json"
    value = configure_mineru(
        config_path=path, api_url="https://ocr.example", language="ch"
    )
    assert load_mineru_config(path) == value
    assert main(["doctor-configured-mineru", "--config-path", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["available"] is True
    result = parse_pdf_configured_mineru(
        input_pdf, config_path=path, job_dir=input_pdf.parent / "job"
    )
    assert result["status"] == "completed"
    assert (
        json.loads((input_pdf.parent / "job/job.json").read_text())["config"][
            "language"
        ]
        == "ch"
    )


def test_profile_can_be_read_when_local_runtime_is_unavailable(tmp_path):
    from ac_document import (
        configure_mineru,
        load_mineru_config,
        doctor_configured_mineru,
    )

    path = tmp_path / "config.json"
    configure_mineru(config_path=path, executable="/missing/mineru")
    assert load_mineru_config(path)["executable"] == "/missing/mineru"
    with pytest.raises(PDFSourceBundleError):
        doctor_configured_mineru(config_path=path)


def test_pause_during_health_prevents_upload(input_pdf, server, monkeypatch):
    from ac_jobs import StoppedError

    paused = []
    original = runner._health

    def health(*args):
        original(*args)
        paused.append(True)

    monkeypatch.setattr(runner, "_health", health)

    def checkpoint():
        if paused:
            raise StoppedError("Paused during health")

    with pytest.raises(StoppedError):
        run(input_pdf, checkpoint=checkpoint)
    assert [r.method for r in server[0]] == ["GET"]
    assert (
        json.loads((input_pdf.parent / "job/job.json").read_text())["status"]
        == "prepared"
    )


def test_pause_during_download_preserves_task_for_resume(input_pdf, server):
    from ac_jobs import StoppedError

    paused, consumed = [], []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            paused.append(True)
            for chunk in (b"first", b"second"):
                consumed.append(chunk)
                yield chunk

    server[1]["stream"] = Stream()

    def checkpoint():
        if paused:
            raise StoppedError("Pause download")

    with pytest.raises(StoppedError):
        run(input_pdf, checkpoint=checkpoint)
    assert consumed == [b"first"]
    assert (
        json.loads((input_pdf.parent / "job/job.json").read_text())["status"]
        == "submitted"
    )
    assert not list((input_pdf.parent / "job").glob(".download-*"))
    server[1]["stream"] = None
    assert run(input_pdf)["status"] == "completed"
    assert sum(r.method == "POST" for r in server[0]) == 1


def test_local_failure_preserves_bounded_redacted_diagnostics(input_pdf, monkeypatch):
    monkeypatch.setenv('TEST_OCR_API_KEY', 'synthetic-private-key')
    executable = Path(fake_executable(input_pdf.parent, fail=True))
    executable.write_text(executable.read_text().replace(
        'if True: raise SystemExit(1)',
        "if True:\n    print('x' * 100000, file=sys.stderr)\n    print('synthetic-private-key bearer fake-token final-error', file=sys.stderr)\n    raise SystemExit(7)"))
    root = input_pdf.parent / 'job'
    with pytest.raises(PDFSourceBundleError, match='exit code 7'):
        parse_pdf_mineru(input_pdf, job_dir=root, executable=str(executable))
    state = json.loads((root / 'local-ocr.json').read_text())
    assert state['exit_code'] == 7
    log = root / state['log']
    text = log.read_text()
    assert 'final-error' in text
    assert 'synthetic-private-key' not in text and 'fake-token' not in text
    assert len(log.read_bytes()) <= 65536
    assert log.stat().st_mode & 0o777 == 0o600


def test_failed_local_job_stays_failed_without_resubmission(input_pdf):
    executable = fake_executable(input_pdf.parent, fail=True)
    root = input_pdf.parent / 'job'
    for _ in range(2):
        with pytest.raises(PDFSourceBundleError):
            parse_pdf_mineru(input_pdf, job_dir=root, executable=executable)
        assert json.loads((root / 'job.json').read_text())['status'] == 'failed'
    diagnostic = json.loads((root / 'local-ocr.json').read_text())
    assert diagnostic['diagnostic'] == 'provider_error_without_details'


def test_local_failure_diagnostics_distinguish_timeout_memory_and_signal():
    from ac_document.mineru_runner import _local_failure_kind
    assert _local_failure_kind('httpx.ReadTimeout', 1) == 'timeout'
    assert _local_failure_kind('RuntimeError: out of memory', 1) == 'memory_exhausted'
    assert _local_failure_kind('', -9) == 'signal_9'
