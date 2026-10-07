from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ac_jobs import InstallationOperation, InstallationCommandError, IdempotencyConflictError, RunBusyError

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Inherited POSIX installation lease')


def wait_for(predicate, timeout=10):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():return
        time.sleep(.02)
    raise AssertionError('installation fixture did not reach state')


def test_status_is_readonly_and_identity_is_stable(tmp_path):
    op=InstallationOperation(tmp_path/'op',{'source':'fixed','destination':'fixture'})
    assert op.status()['state']=='not_started'
    assert not (tmp_path/'op').exists()
    assert InstallationOperation(tmp_path/'op',{'destination':'fixture','source':'fixed'}).operation_id==op.operation_id


def test_commands_stream_redacted_output_and_complete(tmp_path):
    op=InstallationOperation(tmp_path/'op',{'source':'fixed'})
    with op.begin():
        op.checkpoint('downloaded',sha256='fixture')
        result=op.run([sys.executable,'-c','import sys;print("https://secret@example.com/archive");print("中文",file=sys.stderr)'])
        assert 'secret' not in result['stdout'] and '[REDACTED]' in result['stdout']
        assert result['stderr'].strip()=='中文'
        op.complete({'checked':True})
    status=op.status()
    assert status['state']=='succeeded' and status['current']['result']=={'checked':True}
    assert status['commands'][0]['streams_complete']


def test_active_operation_blocks_competitors_and_changed_source(tmp_path):
    op=InstallationOperation(tmp_path/'op',{'source':'fixed'})
    with op.begin():
        other=InstallationOperation(tmp_path/'op',{'source':'fixed'})
        assert other.status()['state']=='running'
        with pytest.raises(RunBusyError):
            with other.begin(retry=True):pass
        changed=InstallationOperation(tmp_path/'op',{'source':'changed'})
        assert changed.status()['lease']['state']=='occupied'
        with pytest.raises(RunBusyError):
            with changed.begin(retry=True):pass
        op.complete({})
    before=(tmp_path/'op/state.json').read_bytes()
    with pytest.raises(IdempotencyConflictError):
        with changed.begin(retry=True):pass
    assert (tmp_path/'op/state.json').read_bytes()==before


def test_failed_attempt_retained_and_retry_requires_explicit_choice(tmp_path):
    op=InstallationOperation(tmp_path/'op',{'source':'fixed'})
    with pytest.raises(InstallationCommandError):
        with op.begin():
            op.run([sys.executable,'-c','import sys;print("failure evidence");sys.exit(7)'])
    first=op.status()
    assert first['state']=='failed' and first['commands'][0]['returncode']==7
    with pytest.raises(RuntimeError,match='explicitly retry'):
        with op.begin():pass
    with op.begin(retry=True):op.complete({'retried':True})
    assert len(list((tmp_path/'op/attempts').iterdir()))==2
    assert 'failure evidence' in next((tmp_path/'op/attempts'/first['current']['attempt_id']).glob('commands/*/stdout.log')).read_text()


def test_command_timeout_has_terminal_receipt(tmp_path):
    op=InstallationOperation(tmp_path/'op',{'source':'fixed'})
    with pytest.raises(InstallationCommandError,match='timed_out'):
        with op.begin():op.run([sys.executable,'-c','import time;time.sleep(20)'],timeout=.2)
    assert op.status()['commands'][0]['state']=='timed_out'


def test_coordinator_kill_leaves_supervisor_queryable_and_retry_safe(tmp_path):
    op=InstallationOperation(tmp_path/'op',{'source':'fixed'})
    script=tmp_path/'owner.py'
    script.write_text('''import sys
from pathlib import Path
from ac_jobs import InstallationOperation
op=InstallationOperation(Path(sys.argv[1])/"op",{"source":"fixed"})
with op.begin():
 op.checkpoint("package_install")
 op.run([sys.executable,"-c","import pathlib,time;root=pathlib.Path("+repr(sys.argv[1])+");(root/'started').touch();print('streamed',flush=True);\\nwhile not (root/'release').exists():time.sleep(.02)\\nprint('finished',flush=True)"])
 op.complete({"checked":True})
''')
    process=subprocess.Popen([sys.executable,str(script),str(tmp_path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        wait_for(lambda:(tmp_path/'started').exists())
        wait_for(lambda:any('streamed' in p.read_text() for p in (tmp_path/'op/attempts').glob('*/commands/*/stdout.log')))
        process.kill();process.wait(timeout=5)
        assert op.status()['state']=='running'
        with pytest.raises(RunBusyError):
            with op.begin(retry=True):pass
        (tmp_path/'release').touch()
        wait_for(lambda:op.status()['lease']['state']=='available')
        old=op.status()
        assert old['state']=='interrupted' and old['commands'][0]['state']=='completed'
        with op.begin(retry=True):op.complete({'recovered':True})
        assert op.status()['state']=='succeeded'
        assert len(list((tmp_path/'op/attempts').iterdir()))==2
    finally:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait(timeout=5)


def test_descendant_with_pipe_and_lease_prevents_false_success(tmp_path):
    op=InstallationOperation(tmp_path/'op',{'source':'fixed'})
    # Child closes stderr but retains stdout and inherited lease past parent's exit.
    program="import os,time;pid=os.fork();\nif pid==0:os.close(2);time.sleep(7);os._exit(0)\nelse:os._exit(0)"
    with pytest.raises(InstallationCommandError,match='outcome_unknown'):
        with op.begin():op.run([sys.executable,'-c',program])
    assert op.status()['lease']['state']=='occupied'
    with pytest.raises(RunBusyError):
        with op.begin(retry=True):pass
    wait_for(lambda:op.status()['lease']['state']=='available')
    assert op.status()['state']=='failed'


@pytest.mark.parametrize("payload", ["[]", "{}"])
def test_corrupt_state_is_not_replaced_by_retry(tmp_path, payload):
    op=InstallationOperation(tmp_path/'op',{'source':'fixed'})
    op.directory.mkdir()
    (op.directory/'state.json').write_text(payload)
    assert op.status()['state']=='unverifiable'
    with pytest.raises(Exception,match='Unreadable installation state'):
        with op.begin(retry=True):pass
    assert (op.directory/'state.json').read_text()==payload


def test_log_failure_cannot_be_reported_as_command_success(tmp_path, monkeypatch):
    import io
    from ac_jobs import installation_worker
    op=InstallationOperation(tmp_path/'op',{'source':'fixed'})
    directory=tmp_path/'command';directory.mkdir()
    original=Path.open
    class BrokenLog:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def write(self,*args):raise OSError('fixture disk full')
        def flush(self):pass
    def patched(path,*args,**kwargs):
        if path.name=='stdout.log' and args and args[0]=='w':return BrokenLog()
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',patched)
    spec=json.dumps({'command':[sys.executable,'-c','print("output")'],'timeout':2}).encode()
    monkeypatch.setattr(sys,'stdin',type('Input',(),{'buffer':io.BytesIO(spec)})())
    with op.begin():
        monkeypatch.setattr(sys,'argv',['worker',str(directory),str(op.fd)])
        assert installation_worker.main()==1
    result=json.loads((directory/'result.json').read_text())
    assert result['state']=='logging_failed' and result['stream_errors']
