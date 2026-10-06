from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ac_jobs import RunStatus
from ac_llm import HostAuthority, HostCoordinator, HostTaskService, LLMClient, LLMCompleted, LLMExecutionOptions
from .test_host_provider import request, response


DRIVER = '''
import json, sys, threading
from ac_llm import *
import ac_llm.host_tasks as tasks
from ac_llm.executor import LLMTaskExecutor
root, phase = sys.argv[1:]
options = LLMExecutionOptions(host_coordinator=HostCoordinator("fake-host", fresh_context=True), host_authority=HostAuthority.UNRESTRICTED)
def barrier():
    print("durable", flush=True)
    threading.Event().wait(30)
if phase == "export":
    original = tasks.prepare_host_task
    def prepare(*args, **kwargs):
        value = original(*args, **kwargs)
        barrier()
        return value
    tasks.prepare_host_task = prepare
    req = LLMRequest("host-task", "Return an answer.", JsonOutput({"type":"object","properties":{"answer":{"type":"number"}},"required":["answer"],"additionalProperties":False}), ModelSelection("host"))
    LLMClient().generate(req, run_root=root, run_id="host-run", options=options)
elif phase == "submit":
    task = HostTaskService().pending(run_root=root, run_id="host-run")[0]
    value = {"schema_version":"ac.llm.host_response.v1","task_id":task["task_id"],"request_sha256":task["request_sha256"],"actor":{"actor_id":"offline","kind":"fake","context_id":task["task_id"]},"output":{"answer":42}}
    HostTaskService().submit(run_root=root, run_id="host-run", response=value)
    barrier()
else:
    method = "_consume_candidates" if phase == "raw" else "_advance_session"
    original = getattr(LLMTaskExecutor, method)
    def interrupted(*args, **kwargs):
        if phase == "raw":
            barrier()
            return original(*args, **kwargs)
        value = original(*args, **kwargs)
        barrier()
        return value
    setattr(LLMTaskExecutor, method, interrupted)
    LLMClient().resume(run_root=root, run_id="host-run", options=options)
'''


def interrupt(root: Path, phase: str) -> None:
    process = subprocess.Popen([sys.executable, "-c", DRIVER, str(root), phase],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    messages = queue.Queue()
    threading.Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True).start()
    try:
        message = messages.get(timeout=10)
        if message.strip() != "durable":
            raise AssertionError(process.communicate(timeout=5))
        process.terminate()
        process.wait(timeout=5)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize("phase", ["export", "submit", "raw", "session"])
def test_real_process_interruption_reuses_durable_host_task(tmp_path, phase):
    options = LLMExecutionOptions(host_coordinator=HostCoordinator("fake-host", fresh_context=True), host_authority=HostAuthority.UNRESTRICTED)
    client, service = LLMClient(), HostTaskService()
    if phase == "export":
        interrupt(tmp_path, phase)
    else:
        client.generate(request(), run_root=tmp_path, run_id="host-run", options=options)
    rows = service.pending(run_root=tmp_path, run_id="host-run", include_completed=True)
    assert len(rows) == 1
    task = service.export(run_root=tmp_path, run_id="host-run", task_id=rows[0]["task_id"])
    if phase == "submit":
        interrupt(tmp_path, phase)
    else:
        service.submit(run_root=tmp_path, run_id="host-run", response=response(task))
    if phase in {"raw", "session"}:
        interrupt(tmp_path, phase)
    result = client.resume(run_root=tmp_path, run_id="host-run", options=options)
    assert result.snapshot.status is RunStatus.SUCCEEDED, result.outcome
    assert isinstance(result.outcome, LLMCompleted) and result.outcome.value == {"answer": 42}
    assert len(service.pending(run_root=tmp_path, run_id="host-run", include_completed=True)) == 1
    assert service.pending(run_root=tmp_path, run_id="host-run") == []
    replay = client.generate(request(), run_root=tmp_path, run_id="host-run", options=options)
    assert replay.outcome.session == result.outcome.session
