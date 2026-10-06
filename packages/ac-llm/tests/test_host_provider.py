from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ac_llm import (
    HostAuthority, HostCoordinator, HostTaskService, IdempotencyConflictError,
    InvalidRequestError, JsonOutput, LLMClient, LLMCompleted, LLMExecutionOptions,
    LLMPaused, LLMRequest, ModelSelection, OutputInvalidError, ProviderDiagnostic,
)
from ac_llm.providers.registry import default_registry


def options(**kwargs):
    return LLMExecutionOptions(
        host_coordinator=HostCoordinator("fake-host", fresh_context=True),
        host_authority=HostAuthority.UNRESTRICTED,
        **kwargs,
    )


def request():
    return LLMRequest("host-task", "Return an answer.", JsonOutput({
        "type": "object", "properties": {"answer": {"type": "number"}},
        "required": ["answer"], "additionalProperties": False,
    }), ModelSelection("host"))


def response(task, *, answer=42, **kwargs):
    return {"schema_version": "ac.llm.host_response.v1", "task_id": task["task_id"],
            "request_sha256": task["request_sha256"],
            "actor": {"actor_id": "offline", "kind": "fake", "context_id": task["task_id"]},
            "output": {"answer": answer}, **kwargs}


def pending(tmp_path, *, req=None, opts=None):
    client = LLMClient()
    opts = options() if opts is None else opts
    req = request() if req is None else req
    result = client.generate(req, run_root=tmp_path, run_id="host-run", options=opts)
    assert isinstance(result.outcome, LLMPaused), result.outcome
    assert result.outcome.details["code"] == "awaiting_host"
    service = HostTaskService()
    rows = service.pending(run_root=tmp_path, run_id="host-run")
    assert len(rows) == 1
    task = service.export(run_root=tmp_path, run_id="host-run", task_id=rows[0]["task_id"])
    return client, service, task, opts


def test_export_submit_resume_and_completed_replay(tmp_path):
    client, service, task, opts = pending(tmp_path)
    assert task["request"]["task_prompt"] == "Return an answer."
    assert task["request"]["model_requirement"]["unavailable_model_policy"] == "use_host_model"
    assert isinstance(client.resume(run_root=tmp_path, run_id="host-run", options=opts).outcome, LLMPaused)
    assert service.submit(run_root=tmp_path, run_id="host-run", response=response(task))["reused"] is False
    assert service.submit(run_root=tmp_path, run_id="host-run", response=response(task))["reused"] is True
    result = client.resume(run_root=tmp_path, run_id="host-run", options=opts)
    assert isinstance(result.outcome, LLMCompleted), result.outcome
    assert result.outcome.value == {"answer": 42}
    assert result.outcome.provider == "host" and result.outcome.model is None
    assert result.outcome.usage is None
    assert service.pending(run_root=tmp_path, run_id="host-run") == []
    replay = client.generate(request(), run_root=tmp_path, run_id="host-run", options=opts)
    assert isinstance(replay.outcome, LLMCompleted)
    assert service.submit(run_root=tmp_path, run_id="host-run", response=response(task))["reused"]
    assert len(service.pending(run_root=tmp_path, run_id="host-run", include_completed=True)) == 1


@pytest.mark.parametrize("changes", [
    {"task_id": "host-wrong"}, {"request_sha256": "0" * 64}, {"output": {"answer": "bad"}},
    {"output": {"answer": float("inf")}}, {"unknown": True}, {"actual_model": 12},
])
def test_invalid_submission_is_rejected(tmp_path, changes):
    _, service, task, _ = pending(tmp_path)
    value = response(task)
    value.update(changes)
    with pytest.raises((InvalidRequestError, OutputInvalidError)):
        service.submit(run_root=tmp_path, run_id="host-run", response=value)
    assert service.pending(run_root=tmp_path, run_id="host-run")[0]["status"] == "awaiting_host"


def test_strict_json_and_conflicting_resubmission(tmp_path):
    _, service, task, _ = pending(tmp_path)
    encoded = json.dumps(response(task))
    with pytest.raises(InvalidRequestError, match="duplicate"):
        service.submit(run_root=tmp_path, run_id="host-run", response=encoded.replace('"answer": 42', '"answer": 42, "answer": 43'))
    with pytest.raises(InvalidRequestError):
        service.submit(run_root=tmp_path, run_id="host-run", response=encoded[:-1])
    service.submit(run_root=tmp_path, run_id="host-run", response=encoded)
    with pytest.raises(IdempotencyConflictError):
        service.submit(run_root=tmp_path, run_id="host-run", response=response(task, answer=43))


def test_brokered_host_turn_preserves_full_output_contract(tmp_path):
    opts = replace(options(), host_authority=HostAuthority.UNKNOWN)
    client, service, task, opts = pending(tmp_path, opts=opts)
    value = response(task)
    with pytest.raises(OutputInvalidError):
        service.submit(run_root=tmp_path, run_id="host-run", response=value)
    value["output"] = {"schema_version": "ac.llm.host_turn.v1", "state": "complete",
                       "result": {"answer": 42}, "host_request": None}
    service.submit(run_root=tmp_path, run_id="host-run", response=value)
    result = client.resume(run_root=tmp_path, run_id="host-run", options=opts)
    assert isinstance(result.outcome, LLMCompleted), result.outcome
    assert result.outcome.value == {"answer": 42}


def test_task_identity_and_resume_survive_directory_copy(tmp_path):
    root = tmp_path / "original"
    _, service, task, opts = pending(root)
    destination = tmp_path / "moved"
    shutil.copytree(root, destination)
    exported = service.export(run_root=destination, run_id="host-run", task_id=task["task_id"])
    assert exported == task
    service.submit(run_root=destination, run_id="host-run", response=response(task))
    result = LLMClient().resume(run_root=destination, run_id="host-run", options=opts)
    assert isinstance(result.outcome, LLMCompleted), result.outcome


def test_host_handoff_does_not_acquire_provider_gate(tmp_path, monkeypatch):
    from ac_llm.executor import LLMTaskExecutor
    monkeypatch.setattr(LLMTaskExecutor, "_provider_gate", lambda *args: pytest.fail("host acquired provider gate"))
    pending(tmp_path)


def test_native_fallback_is_prelaunch_and_requires_coordinator(tmp_path, monkeypatch):
    from ac_llm.executor import LLMTaskExecutor
    registry = default_registry()
    adapter = registry.create("codex")
    monkeypatch.setattr(adapter, "doctor", lambda: ProviderDiagnostic("codex", False, None, prelaunch_unavailable=True))
    monkeypatch.setattr(adapter, "start", lambda *args: pytest.fail("unavailable CLI was started"))
    registry._factories["codex"] = lambda: adapter
    executor = LLMTaskExecutor(registry)
    req = replace(request(), model=ModelSelection("codex", model="requested-model", reasoning_effort="high"))
    fallback_options = replace(options(), host_coordinator=HostCoordinator("codex", default_provider="codex"))
    assert executor._resolve_model(req, options=fallback_options).provider == "host"
    assert executor._resolve_model(req, options=replace(fallback_options, host_coordinator=replace(fallback_options.host_coordinator, native_fallback=False))).provider == "codex"
    client = LLMClient(registry=registry)
    result = client.generate(req, run_root=tmp_path, run_id="fallback", options=fallback_options)
    assert isinstance(result.outcome, LLMPaused), result.outcome
    service = HostTaskService()
    task_id = service.pending(run_root=tmp_path, run_id="fallback")[0]["task_id"]
    task = service.export(run_root=tmp_path, run_id="fallback", task_id=task_id)
    assert task["route"]["fallback_reason"] == "provider_unavailable_before_start"
    service.submit(run_root=tmp_path, run_id="fallback", response=response(task, actual_model="host-model"))
    result = client.resume(run_root=tmp_path, run_id="fallback", options=fallback_options)
    assert isinstance(result.outcome, LLMCompleted) and result.outcome.model == "host-model"


def test_host_session_exports_accepted_history(tmp_path):
    from ac_jobs import RunContext, RunRepository, RunSpec
    from ac_llm import LLMTaskService
    repository = RunRepository(tmp_path)
    context = RunContext(repository, repository.create(RunSpec("host-run", "host-session", {})), resume_input=None)
    service, llm, opts = HostTaskService(), LLMTaskService(), options()
    paused = llm.execute(context, request(), options=opts)
    task = service.export(run_root=tmp_path, run_id="host-run", task_id=paused.details["host_task_id"])
    service.submit(run_root=tmp_path, run_id="host-run", response=response(task))
    first = llm.resume(context, request().task_id, options=opts)
    second_request = replace(request(), task_id="next-task", prompt="Continue the previous answer.", session=first.session)
    outcome = llm.execute(context, second_request, options=opts)
    assert isinstance(outcome, LLMPaused), outcome
    exported = service.export(run_root=tmp_path, run_id="host-run", task_id=outcome.details["host_task_id"])
    assert exported["request"]["session_history"] == [{"task_id": "host-task", "prompt": "Return an answer.",
                                                        "inputs": [], "result": {"answer": 42}, "accepted_prefix_sha256": first.session.accepted_prefix_sha256}]


def test_broker_continuation_and_duplicate_recovery_export_distinct_tasks(tmp_path):
    from ac_llm import HostResponse, HostResponseStatus

    class Broker:
        calls = 0
        execution_identity = {"kind": "offline-test", "version": 1}

        def execute(self, request, *, workspace):
            self.calls += 1
            return HostResponse(HostResponseStatus.COMPLETED, result="verified evidence")

    broker = Broker()
    opts = replace(options(), host_authority=HostAuthority.UNKNOWN, host_broker=broker,
                   task_binding={"loop_id": "loop", "worker_id": "a", "role": "proposer", "fresh_context_required": True})
    client, service, task, opts = pending(tmp_path, opts=opts)
    turn = {"schema_version": "ac.llm.host_turn.v1", "state": "request_host", "result": None,
            "host_request": {"request_id": "evidence", "instruction": "Read the evidence.", "purpose": "Check answer"}}

    def submit_turn(task, output):
        value = response(task, isolation="fresh_context")
        value["actor"]["context_id"] = "worker-a"
        value["output"] = output
        service.submit(run_root=tmp_path, run_id="host-run", response=value)

    submit_turn(task, turn)
    result = client.resume(run_root=tmp_path, run_id="host-run", options=opts)
    assert isinstance(result.outcome, LLMPaused), result.outcome
    next_task = service.export(run_root=tmp_path, run_id="host-run", task_id=result.outcome.details["host_task_id"])
    assert next_task["request"]["continuation"] is not None
    assert next_task["request"]["host_history"][0]["request"]["host_request"]["instruction"] == "Read the evidence."
    submit_turn(next_task, turn)
    result = client.resume(run_root=tmp_path, run_id="host-run", options=opts)
    assert isinstance(result.outcome, LLMPaused), result.outcome
    recovery = service.export(run_root=tmp_path, run_id="host-run", task_id=result.outcome.details["host_task_id"])
    assert recovery["task_id"] != next_task["task_id"]
    assert recovery["request"]["recovery"] is not None
    submit_turn(recovery, {"schema_version": "ac.llm.host_turn.v1", "state": "complete", "result": {"answer": 42}, "host_request": None})
    completed = client.resume(run_root=tmp_path, run_id="host-run", options=opts)
    assert isinstance(completed.outcome, LLMCompleted), completed.outcome
    assert broker.calls == 1


def test_native_runtime_failures_do_not_fallback(tmp_path, adapter, registry):
    from ac_llm import FailureCategory, ProviderFailure
    from ac_llm.providers.host import HostAdapter

    registry.register("host", HostAdapter)
    opts = replace(options(), host_coordinator=HostCoordinator("codex", default_provider="codex"))
    adapter.steps.append(ProviderFailure("authentication refused", category=FailureCategory.AUTHENTICATION))
    result = LLMClient(registry=registry).generate(replace(request(), model=ModelSelection("codex")), run_root=tmp_path,
                                                run_id="native-failure", options=opts)
    assert isinstance(result.outcome, LLMPaused) and result.outcome.details["code"] == "authentication"
    assert adapter.start_calls == 1
    assert HostTaskService().pending(run_root=tmp_path, run_id="native-failure") == []


def test_stop_rejects_new_host_submission(tmp_path):
    from ac_jobs import RunRepository, StoppedError

    client, service, task, opts = pending(tmp_path)
    RunRepository(tmp_path).request_stop("host-run", reason="user stop")
    with pytest.raises(StoppedError):
        service.submit(run_root=tmp_path, run_id="host-run", response=response(task))
    assert service.pending(run_root=tmp_path, run_id="host-run")[0]["status"] == "stopped"
    continued = client.resume(run_root=tmp_path, run_id="host-run", options=opts)
    assert continued.snapshot.attempt == 2
    service.submit(run_root=tmp_path, run_id="host-run", response=response(task))
    assert isinstance(client.resume(run_root=tmp_path, run_id="host-run", options=opts).outcome, LLMCompleted)


def test_cli_diagnoses_host_route_and_rejects_malformed_config(tmp_path, monkeypatch, capsys):
    from ac_llm.cli import main

    monkeypatch.setenv("AC_LLM_HOST_COORDINATOR", json.dumps(HostCoordinator("work").to_document()))
    assert main(["doctor"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["data"]["provider"] == "host" and document["data"]["available"]
    monkeypatch.setenv("AC_LLM_HOST_COORDINATOR", '{"coordinator_id":')
    assert main(["doctor"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["error"]["code"] == "invalid_request"


def test_history_inputs_are_verified_and_materialized(tmp_path):
    import base64
    from ac_jobs import ArtifactSourceRef, RunContext, RunRepository, RunSpec
    from ac_llm import LLMInputArtifact, LLMTaskService

    repo = RunRepository(tmp_path)
    context = RunContext(repo, repo.create(RunSpec("history", "history-inputs", {})), resume_input=None)
    source = context.artifacts.publish_bytes("document/source", b"remembered evidence", media_type="text/plain")
    input = LLMInputArtifact("source", ArtifactSourceRef("history", source.artifact_id, source.digest), "text/plain")
    llm, service, opts = LLMTaskService(), HostTaskService(), options()
    req = replace(request(), inputs=(input,))
    paused = llm.execute(context, req, options=opts)
    task = service.export(run_root=tmp_path, run_id="history", task_id=paused.details["host_task_id"])
    service.submit(run_root=tmp_path, run_id="history", response=response(task))
    first = llm.resume(context, req.task_id, options=opts)
    next_req = replace(request(), task_id="history-next", session=first.session)
    next_pause = llm.execute(context, next_req, options=opts)
    task = service.export(run_root=tmp_path, run_id="history", task_id=next_pause.details["host_task_id"])
    remembered = task["request"]["session_history"][0]["inputs"][0]
    assert base64.b64decode(remembered["content_base64"]) == b"remembered evidence"
    folder = tmp_path / "public-export"
    service.materialize(run_root=tmp_path, run_id="history", task_id=task["task_id"], directory=folder)
    assert (folder / "history/session/0/inputs/0000-source.txt").read_bytes() == b"remembered evidence"
    assert json.loads((folder / "task.json").read_text()) == task


def test_concurrent_independent_workers_cannot_share_context(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from ac_jobs import RunContext, RunRepository, RunSpec
    from ac_llm import LLMTaskService

    repo = RunRepository(tmp_path)
    context = RunContext(repo, repo.create(RunSpec("independent", "worker-contexts", {})), resume_input=None)
    service, llm = HostTaskService(), LLMTaskService()
    tasks = []
    for worker in ("a", "b"):
        opts = options(task_binding={"loop_id": "comparison", "worker_id": worker, "role": "proposer", "fresh_context_required": True})
        paused = llm.execute(context, replace(request(), task_id=f"worker-{worker}"), options=opts)
        task = service.export(run_root=tmp_path, run_id="independent", task_id=paused.details["host_task_id"])
        value = response(task, isolation="fresh_context")
        value["actor"]["context_id"] = "same-context"
        tasks.append(value)

    def submit(value):
        try:
            service.submit(run_root=tmp_path, run_id="independent", response=value)
            return True
        except InvalidRequestError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, tasks)) == [False, True]


def test_existing_unusable_cli_does_not_enable_host_fallback(tmp_path):
    from ac_llm.providers.codex import CodexAdapter

    missing = CodexAdapter(binary=str(tmp_path / "missing"))
    assert missing.doctor().prelaunch_unavailable
    unusable = tmp_path / "unusable"
    unusable.write_text("not executable")
    unusable.chmod(0o600)
    diagnostic = CodexAdapter(binary=str(unusable)).doctor()
    assert not diagnostic.available and not diagnostic.prelaunch_unavailable


def test_materialize_rejects_external_work_symlink_before_writing(tmp_path):
    _, service, task, _ = pending(tmp_path)
    root, outside = tmp_path / "export", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "work").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(InvalidRequestError, match="real directory"):
        service.materialize(run_root=tmp_path, run_id="host-run", task_id=task["task_id"], directory=root)
    assert not (root / "task.json").exists()
    assert list(outside.iterdir()) == []


def test_formatter_child_can_handoff_without_replaying_native_worker(tmp_path, adapter, registry):
    from ac_llm import LLMTaskService, ProviderExecution, ProviderTerminalKind
    from ac_llm.output import CandidateMaterial
    from ac_llm.providers.host import HostAdapter

    registry.register("host", HostAdapter)
    native_start = adapter.start
    launched = False

    def start(*args, **kwargs):
        nonlocal launched
        launched = True
        return native_start(*args, **kwargs)

    adapter.start = start
    adapter.doctor = lambda: ProviderDiagnostic("codex", not launched, None, prelaunch_unavailable=launched)
    adapter.steps.append(ProviderExecution(ProviderTerminalKind.COMPLETED,
                         (CandidateMaterial(value={"answer_text": "the complete answer is present"}, terminal=True),)))
    req = LLMRequest("format-host", "Return the answer.", JsonOutput({"type": "object", "required": ["answer"]}), ModelSelection("codex"))
    opts = replace(options(), host_coordinator=HostCoordinator("codex", default_provider="codex"))
    client = LLMClient(service=LLMTaskService(registry=registry))
    first = client.generate(req, run_root=tmp_path, run_id="format-run", options=opts)
    assert isinstance(first.outcome, LLMPaused), first.outcome
    service = HostTaskService()
    row = service.pending(run_root=tmp_path, run_id="format-run")[0]
    assert row["binding"]["role"] == "formatter"
    task = service.export(run_root=tmp_path, run_id="format-run", task_id=row["task_id"])
    value = response(task)
    value["output"] = {"action": "format", "reason": "content exists", "formatted_output": '{"answer":"the complete answer is present"}'}
    service.submit(run_root=tmp_path, run_id="format-run", response=value)
    completed = client.resume(run_root=tmp_path, run_id="format-run", options=opts)
    assert isinstance(completed.outcome, LLMCompleted), completed.outcome
    assert completed.outcome.value == {"answer": "the complete answer is present"}
    assert adapter.start_calls == 1


def test_early_broker_files_survive_later_workspace_overwrite(tmp_path):
    import base64
    from ac_llm import HostResponse, HostResponseStatus

    class Broker:
        execution_identity = {"kind": "file-fixture"}

        def execute(self, request, *, workspace):
            content = request.request_id.encode()
            (workspace / "work/result.txt").write_bytes(content)
            return HostResponse(HostResponseStatus.COMPLETED, result=request.request_id, files=("work/result.txt",))

    opts = replace(options(), host_authority=HostAuthority.UNKNOWN, host_broker=Broker())
    client, service, task, opts = pending(tmp_path, opts=opts)
    for request_id in ("first", "second"):
        value = response(task)
        value["output"] = {"schema_version": "ac.llm.host_turn.v1", "state": "request_host", "result": None,
                           "host_request": {"request_id": request_id, "instruction": "Read source.", "purpose": "verify"}}
        service.submit(run_root=tmp_path, run_id="host-run", response=value)
        result = client.resume(run_root=tmp_path, run_id="host-run", options=opts)
        assert isinstance(result.outcome, LLMPaused), result.outcome
        task = service.export(run_root=tmp_path, run_id="host-run", task_id=result.outcome.details["host_task_id"])
    assert [base64.b64decode(item["files"][0]["content_base64"]) for item in task["request"]["host_history"]] == [b"first", b"second"]
    folder = tmp_path / "exported"
    service.materialize(run_root=tmp_path, run_id="host-run", task_id=task["task_id"], directory=folder)
    assert (folder / "history/host/1/work/result.txt").read_bytes() == b"first"
    assert (folder / "work/result.txt").read_bytes() == b"second"
