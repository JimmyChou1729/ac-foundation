from dataclasses import replace
import hashlib
from pathlib import Path
import shutil

import pytest

from ac_jobs import RunRepository
from ac_llm import (
    HostAuthority, HostCoordinator, HostTaskService, JsonOutput, LLMClient,
    LLMExecutionOptions, LLMPaused, LLMRequest, ModelSelection,
    with_host_resume_location,
)
from ac_llm.executor import LLMTaskExecutor


def pending(tmp_path, monkeypatch=None):
    root = tmp_path / 'jobs'
    options = LLMExecutionOptions(host_coordinator=HostCoordinator('fixture'),
                                  host_authority=HostAuthority.UNRESTRICTED)
    client = LLMClient()
    if monkeypatch is not None:
        original = LLMTaskExecutor._pause
        def legacy(self, *args, **kwargs):
            details = dict(kwargs.get('details') or {})
            details.pop('run_root', None)
            details.pop('run_id', None)
            kwargs['details'] = details
            return original(self, *args, **kwargs)
        monkeypatch.setattr(LLMTaskExecutor, '_pause', legacy)
    request = LLMRequest('answer', 'Return answer 42.', JsonOutput({
        'type': 'object', 'properties': {'answer': {'type': 'integer'}},
        'required': ['answer'], 'additionalProperties': False,
    }), ModelSelection('host'))
    result = client.generate(request, run_root=root, run_id='public-host', options=options)
    return root, client, result, options


def locate(details):
    return {'run_root': details['run_root'], 'run_id': details['run_id'],
            'task_id': details['host_task_id']}


def test_new_host_pause_can_export_using_only_returned_location(tmp_path):
    root, client, result, options = pending(tmp_path)
    details = result.outcome.details
    assert details['run_root'] == str(root.resolve())
    assert details['run_id'] == result.snapshot.run_id
    assert details['response_contract'] == 'ac.llm.host_response.v1'
    exported = HostTaskService().export(**locate(details))
    assert exported['run_id'] == result.snapshot.run_id
    again = client.resume(run_root=details['run_root'], run_id=details['run_id'], options=options)
    assert locate(again.outcome.details) == locate(details)
    assert HostTaskService().export(**locate(again.outcome.details))['request_sha256'] == exported['request_sha256']


@pytest.mark.parametrize('relocated', [False, True])
def test_legacy_status_location_is_readonly_and_uses_current_repository(tmp_path, monkeypatch, relocated):
    root, client, result, options = pending(tmp_path, monkeypatch)
    assert 'run_root' not in result.outcome.details
    monkeypatch.undo()
    if relocated:
        moved = tmp_path / 'moved-jobs'
        shutil.copytree(root, moved)
        root = moved
    repository = RunRepository(root)
    snapshot = repository.inspect(result.snapshot.run_id).snapshot
    def digests():
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}
    before = digests()
    located = with_host_resume_location(snapshot, run_root=repository.root)
    assert digests() == before
    assert 'run_root' not in snapshot.awaiting.details
    assert located.revision == snapshot.revision
    task = HostTaskService().export(**locate(located.awaiting.details))
    assert task['run_id'] == snapshot.run_id
    replay = client.resume(run_root=root, run_id=snapshot.run_id, options=options)
    assert isinstance(replay.outcome, LLMPaused)
    assert replay.outcome.details['run_root'] == str(root.resolve())
    assert HostTaskService().export(**locate(replay.outcome.details))['request_sha256'] == task['request_sha256']


def test_wrapped_worker_pause_is_located_without_rebinding_child_or_nonhost(tmp_path):
    root, _, result, _ = pending(tmp_path)
    old = {k: v for k, v in result.snapshot.awaiting.details.items() if k not in {'run_id', 'run_root'}}
    old.update(code='proposer_reviewer_worker_paused', llm_code='awaiting_host')
    snapshot = replace(result.snapshot, awaiting=replace(result.snapshot.awaiting, details=old))
    located = with_host_resume_location(snapshot, run_root=root)
    assert located.awaiting.details['run_id'] == snapshot.run_id
    assert located.awaiting.details['llm_code'] == 'awaiting_host'
    foreign = replace(snapshot, awaiting=replace(snapshot.awaiting,
        details={**old, 'run_id': 'explicit-child', 'run_root': '/explicit/child'}))
    assert with_host_resume_location(foreign, run_root=root) is foreign
    other = replace(snapshot, awaiting=replace(snapshot.awaiting, details={'code': 'approval_required'}))
    assert with_host_resume_location(other, run_root=root) is other
