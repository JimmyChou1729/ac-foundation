from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ac_jobs import IdempotencyConflictError as RunConflictError, RunStatus
from ac_llm import HostAuthority, HostCoordinator, HostTaskService, LLMExecutionOptions, IdempotencyConflictError
from ac_document import AcDocumentService, HostPDFRunner, RenderedPDFPage, verify_pdf_source_bundle


class Renderer:
    def __init__(self):
        self.calls = []

    def render_page(self, pdf, number):
        self.calls.append(number)
        width, height = 100 + number, 200
        png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + width.to_bytes(4, "big")
               + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")
        return RenderedPDFPage(number, png, width, height)


class Extractor:
    def __init__(self, pages):
        self.pages, self.calls = pages, 0

    def extract(self, pdf):
        self.calls += 1
        return SimpleNamespace(pages=self.pages)


def runner(tmp_path, pages=("page one", "page two")):
    options = LLMExecutionOptions(host_coordinator=HostCoordinator("fake-host", fresh_context=True),
                                  host_authority=HostAuthority.UNRESTRICTED)
    return HostPDFRunner(tmp_path / "project", renderer=Renderer(), text_extractor=Extractor(pages), options=options)


def page(number, *, coverage="complete", blocks=None, warnings=None):
    return {"schema_version": "ac.document.host_pdf_page.v1", "page_number": number,
            "coverage": coverage, "blocks": blocks if blocks is not None else [
                {"kind": "paragraph", "text": f"Page {number} with 中文", "rows": []}],
            "warnings": warnings or []}


def submit(root, value):
    host = HostTaskService()
    tasks = host.pending(run_root=root, run_id="pdf-test")
    assert len(tasks) == 1
    task = host.export(run_root=root, run_id="pdf-test", task_id=tasks[0]["task_id"])
    response = {"schema_version": "ac.llm.host_response.v1", "task_id": task["task_id"],
                "request_sha256": task["request_sha256"],
                "actor": {"actor_id": "test", "kind": "fake", "context_id": task["task_id"]},
                "output": value}
    host.submit(run_root=root, run_id="pdf-test", response=response)
    assert host.submit(run_root=root, run_id="pdf-test", response=response)["reused"]
    return task, response


def source(tmp_path):
    path = tmp_path / "input.pdf"
    path.write_bytes(b"%PDF-1.4\nOffline recognition fixture\n")
    return path


def run(service, path, tmp_path):
    return service.run(path, output_dir=tmp_path / "bundle", run_id="pdf-test")


def test_multiple_pages_host_resume_replay_and_rich_import(tmp_path):
    path, service = source(tmp_path), runner(tmp_path)
    first = run(service, path, tmp_path)
    assert first.status is RunStatus.PAUSED, first.error
    assert first.awaiting.details["code"] == "awaiting_host"
    assert run(service, path, tmp_path).status is RunStatus.PAUSED
    task, response = submit(service.repository.root, page(1, blocks=[
        {"kind": "heading", "text": "中文 Chapter", "rows": []},
        {"kind": "paragraph", "text": r"Energy \(E\) and reading order.", "rows": []},
        {"kind": "equation", "text": r"E=mc^2", "rows": []},
        {"kind": "table", "text": "Measured values", "rows": [["x", "y"], ["1", "2"]]},
        {"kind": "figure", "text": "Original plot", "rows": []},
    ]))
    assert run(service, path, tmp_path).status is RunStatus.PAUSED
    assert service.renderer.calls == [1, 2]
    # Simulate a fresh coordinator process; previous accepted page and images remain durable.
    resumed = runner(tmp_path)
    submit(resumed.repository.root, page(2))
    last = run(resumed, path, tmp_path)
    assert last.status is RunStatus.SUCCEEDED, last.error
    assert resumed.renderer.calls == [] and resumed.text_extractor.calls == 0
    result = resumed.read_result(last)
    manifest = verify_pdf_source_bundle(result["manifest"])
    assert manifest["provider"]["name"] == "host"
    assert len(manifest["pages"]) == 2
    assert all(e["bbox"] is None for e in manifest["entries"])
    assert (tmp_path / "bundle/original.pdf").read_bytes() == path.read_bytes()
    assert any("full-page" in warning for warning in result["warnings"])
    exported = AcDocumentService(cache_root=tmp_path / "cache").export_rich_document(
        result["source"], output_dir=tmp_path / "rich", pdf_source_manifest=result["manifest"])
    document = json.loads(Path(exported["source"]).read_text())
    assert document["metadata"]["pdf_source"]["proofread"] is False
    assert {item["page_number"] for item in document["page_map"]} == {1, 2}
    assert run(resumed, path, tmp_path).status is RunStatus.SUCCEEDED
    host = HostTaskService()
    assert len(host.pending(run_root=resumed.repository.root, run_id="pdf-test", include_completed=True)) == 2
    assert host.submit(run_root=resumed.repository.root, run_id="pdf-test", response=response)["reused"]


def test_changed_pdf_and_conflicting_submission_rejected(tmp_path):
    path, service = source(tmp_path), runner(tmp_path)
    run(service, path, tmp_path)
    task, response = submit(service.repository.root, page(1))
    response["output"] = page(1, blocks=[{"kind": "paragraph", "text": "changed", "rows": []}])
    with pytest.raises(IdempotencyConflictError):
        HostTaskService().submit(run_root=service.repository.root, run_id="pdf-test", response=response)
    path.write_bytes(b"%PDF-1.4\nDifferent PDF\n")
    with pytest.raises(RunConflictError):
        run(service, path, tmp_path)
    assert service.renderer.calls == [1]


@pytest.mark.parametrize("coverage,blocks,warnings,status", [
    ("empty", [], [], "empty"),
    ("partial", [{"kind": "paragraph", "text": "Visible region", "rows": []}], ["Bottom line illegible"], "partial"),
])
def test_coverage_is_explicit_without_fabricated_boxes(tmp_path, coverage, blocks, warnings, status):
    path, service = source(tmp_path), runner(tmp_path, pages=("",))
    run(service, path, tmp_path)
    submit(service.repository.root, page(1, coverage=coverage, blocks=blocks, warnings=warnings))
    last = run(service, path, tmp_path)
    assert last.status is RunStatus.SUCCEEDED, last.error
    manifest = verify_pdf_source_bundle(service.read_result(last)["manifest"])
    assert manifest["pages"][0]["status"] == status
    assert manifest["resources"][0]["rendered"] is False


def test_semantically_inconsistent_page_is_not_published(tmp_path):
    path, service = source(tmp_path), runner(tmp_path, pages=("",))
    run(service, path, tmp_path)
    submit(service.repository.root, page(1, coverage="empty"))
    last = run(service, path, tmp_path)
    assert last.status is RunStatus.FAILED
    assert last.error.code == "host_pdf_page_invalid"
    assert not (tmp_path / "bundle").exists()


def test_unavailable_host_cannot_claim_success(tmp_path):
    path, service = source(tmp_path), runner(tmp_path, pages=("",))
    run(service, path, tmp_path)
    submit(service.repository.root, page(1, coverage="unavailable", blocks=[], warnings=["Cannot view image"]))
    last = run(service, path, tmp_path)
    assert last.status is RunStatus.FAILED
    assert last.error.code == "host_pdf_no_usable_result"
    assert not (tmp_path / "bundle").exists()


def test_actual_process_interruption_after_next_page_export(tmp_path):
    import os
    import queue
    import subprocess
    import sys
    import threading

    path, service = source(tmp_path), runner(tmp_path)
    run(service, path, tmp_path)
    submit(service.repository.root, page(1))
    driver = r"""
import runpy, sys, threading
from pathlib import Path
from ac_llm import LLMPaused, LLMTaskService
fixture, directory = sys.argv[1:]
namespace = runpy.run_path(fixture)
root = Path(directory)
class Interrupted:
    def execute_or_resume(self, *args, **kwargs):
        outcome = LLMTaskService().execute_or_resume(*args, **kwargs)
        if isinstance(outcome, LLMPaused):
            print("durable", flush=True)
            threading.Event().wait(30)
        return outcome
service = namespace["runner"](root)
service.task_service = Interrupted()
namespace["run"](service, root / "input.pdf", root)
"""
    process = subprocess.Popen([sys.executable, "-c", driver, str(Path(__file__).resolve()), str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
    messages = queue.Queue()
    threading.Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True).start()
    try:
        assert messages.get(timeout=10).strip() == "durable"
        process.kill()
        process.wait(timeout=5)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
    submit(service.repository.root, page(2))
    last = run(runner(tmp_path), path, tmp_path)
    assert last.status is RunStatus.SUCCEEDED, last.error
    assert len(HostTaskService().pending(run_root=service.repository.root, run_id="pdf-test", include_completed=True)) == 2


def test_real_poppler_pdf_and_public_cli_pause_resume(tmp_path, monkeypatch, capsys):
    import shutil
    from ac_document.cli import main

    if not shutil.which("pdftotext") or not shutil.which("pdftoppm"):
        pytest.skip("Poppler is not installed")
    # A complete small PDF with a correct cross-reference table, not an OCR mock.
    stream = b"BT /F1 18 Tf 30 70 Td (Hello OCR) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    payload, offsets = b"%PDF-1.4\n", [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(payload))
        payload += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(payload)
    payload += b"xref\n0 6\n0000000000 65535 f \n"
    payload += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    payload += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path = tmp_path / "real.pdf"
    path.write_bytes(payload)
    monkeypatch.setenv("AC_LLM_HOST_COORDINATOR", json.dumps({"coordinator_id": "fake-host", "default_provider": "host"}))
    args = ["parse-pdf-host", str(path), "--project-dir", str(tmp_path / "project"),
            "--output-dir", str(tmp_path / "bundle"), "--run-id", "pdf-test"]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "paused", first
    service = HostTaskService()
    pending = service.pending(run_root=tmp_path / "project", run_id="pdf-test")
    assert len(pending) == 1
    task = service.export(run_root=tmp_path / "project", run_id="pdf-test", task_id=pending[0]["task_id"])
    response = {"schema_version": "ac.llm.host_response.v1", "task_id": task["task_id"],
                "request_sha256": task["request_sha256"],
                "actor": {"actor_id": "test", "kind": "fake", "context_id": task["task_id"]},
                "output": {"schema_version": "ac.llm.host_turn.v1", "state": "complete",
                           "result": page(1, blocks=[{"kind": "paragraph", "text": "Hello OCR", "rows": []}]), "host_request": None}}
    service.submit(run_root=tmp_path / "project", run_id="pdf-test", response=response)
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed", result
    manifest = verify_pdf_source_bundle(result["data"]["manifest"])
    assert manifest["pages"][0]["size"] == [2000, 1000]


@pytest.mark.parametrize("failure", ["renderer", "publication"])
def test_explicit_retry_preserves_accepted_pages(tmp_path, monkeypatch, failure):
    import ac_document.host_pdf as module

    path, service = source(tmp_path), runner(tmp_path)
    run(service, path, tmp_path)
    submit(service.repository.root, page(1))
    if failure == "renderer":
        original = service.renderer.render_page
        def failed_render(pdf, number):
            if number == 2:
                raise OSError("temporary renderer failure")
            return original(pdf, number)
        service.renderer.render_page = failed_render
    else:
        assert run(service, path, tmp_path).status is RunStatus.PAUSED
        submit(service.repository.root, page(2))
        original = module._publish_bundle
        monkeypatch.setattr(module, "_publish_bundle", lambda *args: (_ for _ in ()).throw(OSError("temporary publication failure")))
    failed = run(service, path, tmp_path)
    assert failed.status is RunStatus.FAILED
    assert run(service, path, tmp_path).status is RunStatus.FAILED
    if failure == "renderer":
        service.renderer.render_page = original
    else:
        monkeypatch.setattr(module, "_publish_bundle", original)
    result = service.run(path, output_dir=tmp_path / "bundle", run_id="pdf-test", retry=True)
    assert result.recovery_epoch == 1
    if failure == "renderer":
        assert result.status is RunStatus.PAUSED, result.error
        tasks = HostTaskService().pending(run_root=service.repository.root, run_id="pdf-test")
        assert len(tasks) == 1 and "page-2" in tasks[0]["llm_task_id"]
        submit(service.repository.root, page(2))
        result = run(service, path, tmp_path)
    assert result.status is RunStatus.SUCCEEDED, result.error
    manifest = verify_pdf_source_bundle(service.read_result(result)["manifest"])
    assert len(manifest["entries"]) == 2


def test_user_stop_requires_explicit_retry(tmp_path):
    from ac_jobs import ResumeReason
    path, service = source(tmp_path), runner(tmp_path)
    first = run(service, path, tmp_path)
    service.repository.request_stop(first.run_id, reason="user stopped")
    stopped = run(service, path, tmp_path)
    assert stopped.status is RunStatus.PAUSED
    assert service.repository.inspect(first.run_id).stop_request is not None
    assert stopped.attempt == first.attempt
    assert run(service, path, tmp_path).attempt == stopped.attempt
    continued = service.run(path, output_dir=tmp_path / "bundle", run_id="pdf-test", retry=True)
    assert continued.status is RunStatus.PAUSED
    assert continued.awaiting.details["code"] == "awaiting_host"


def test_explicit_host_preferences_are_durable(tmp_path):
    path, service = source(tmp_path), runner(tmp_path)
    result = service.run(path, output_dir=tmp_path / "bundle", run_id="pdf-test",
                         model="gpt-example", reasoning_effort="low")
    assert result.status is RunStatus.PAUSED, result.error
    task = HostTaskService().pending(run_root=service.repository.root, run_id="pdf-test")[0]
    exported = HostTaskService().export(run_root=service.repository.root, run_id="pdf-test", task_id=task["task_id"])
    requirement = exported["request"]["model_requirement"]
    assert requirement["model"] == "gpt-example" and requirement["reasoning_effort"] == "low"
    with pytest.raises(RunConflictError):
        run(service, path, tmp_path)
