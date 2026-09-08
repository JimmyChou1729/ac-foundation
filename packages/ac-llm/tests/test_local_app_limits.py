import sys
import time
from pathlib import Path

import pytest

from ac_llm import LLMExecutionOptions, LLMExecutionProfile, ExecutionLimits, ProviderGateOptions, ProviderFailure, InvalidRequestError
from ac_llm.host import AcRuntimeEnvironment
from ac_llm.gate import ProviderCallGate
from ac_llm.providers.process import ProcessRunner


def local(home, **kwargs):
    return LLMExecutionOptions(profile=LLMExecutionProfile.LOCAL_APP,
        runtime_environment=AcRuntimeEnvironment.capture({"AC_HOME": str(home)}), **kwargs)


def test_local_policy_preserves_explicit_scope_and_attempt_cap(tmp_path):
    options = local(tmp_path, gate=ProviderGateOptions(enabled=False, global_limit=100, shared_root=tmp_path/'other'))
    assert options.gate.enabled and options.gate.global_limit == 8
    assert options.gate.shared_root == tmp_path/'other'
    assert local(tmp_path).gate.shared_root is None
    assert options.limits == ExecutionLimits(300, 600)
    high_provider = local(tmp_path, gate=ProviderGateOptions(global_limit=16, provider_limits={"codex":16}))
    assert high_provider.gate.provider_limits["codex"] == 8
    tight = local(tmp_path, limits=ExecutionLimits(2, 3), gate=ProviderGateOptions(global_limit=1))
    assert tight.limits == ExecutionLimits(2, 3) and tight.gate.global_limit == 1
    standard = LLMExecutionOptions(gate=ProviderGateOptions(enabled=False, shared_root=tmp_path/'other'))
    assert not standard.gate.enabled and standard.limits == ExecutionLimits()
    assert standard.gate.shared_root == tmp_path/'other'


@pytest.mark.parametrize('value', [0,-1,float('inf'),float('nan'),True])
def test_invalid_total_timeout(value):
    with pytest.raises(InvalidRequestError):
        ExecutionLimits(total_timeout_seconds=value)


def test_stderr_activity_cannot_extend_wall_limit(tmp_path):
    started = time.monotonic()
    with pytest.raises(ProviderFailure) as exc:
        ProcessRunner().run([sys.executable,'-u','-c','import sys,time\nwhile True:\n sys.stderr.write("noise\\n");sys.stderr.flush();time.sleep(.01)'],
            stdin=b'', env=None, cwd=tmp_path, idle_timeout_seconds=2, total_timeout_seconds=.15, stop_check=lambda:None)
    assert exc.value.details['code'] == 'provider_total_timeout'
    assert time.monotonic()-started < 3


@pytest.mark.parametrize("task_count", [1, 2])
def test_per_run_gate_bounds_processes_without_sharing_tasks(tmp_path, task_count):
    import subprocess
    code = """
import sys,time
from pathlib import Path
from ac_llm import LLMExecutionOptions,LLMExecutionProfile,ProviderGateOptions
from ac_llm.host import AcRuntimeEnvironment
from ac_llm.executor import LLMTaskExecutor
from types import SimpleNamespace
root=Path(sys.argv[1])
run=root/sys.argv[3]
context=SimpleNamespace(run_directory=run,repository=SimpleNamespace(root=root))
options=LLMExecutionOptions(profile=LLMExecutionProfile.LOCAL_APP,
 runtime_environment=AcRuntimeEnvironment.capture({'AC_HOME':str(root)}),
 gate=ProviderGateOptions(minimum_available_memory_fraction=None,memory_launch_interval_seconds=.001))
with LLMTaskExecutor._provider_gate(context,options).acquire('fixture',checkpoint=lambda:None):
 (root/('ready-'+sys.argv[2])).write_text('ready')
 while not (root/'release').exists():time.sleep(.01)
"""
    children = [subprocess.Popen([sys.executable,'-c',code,str(tmp_path),f'{task}-{i}',f'run-{task}'])
                for task in range(task_count) for i in range(9)]
    try:
        deadline = time.monotonic()+10
        while len(list(tmp_path.glob('ready-*'))) < 8*task_count and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(list(tmp_path.glob('ready-*'))) == 8*task_count
        time.sleep(.2)
        assert len(list(tmp_path.glob('ready-*'))) == 8*task_count
        for task in range(task_count):
            assert len(list(tmp_path.glob(f'ready-{task}-*'))) == 8
        (tmp_path/'release').touch()
        for child in children: assert child.wait(timeout=10) == 0
        assert len(list(tmp_path.glob('ready-*'))) == 9*task_count
    finally:
        (tmp_path/'release').touch()
        for child in children:
            if child.poll() is None: child.terminate()
            child.wait(timeout=10)


def test_queue_wait_does_not_consume_execution_deadline(tmp_path):
    import threading
    options = local(tmp_path, limits=ExecutionLimits(.1,.2), gate=ProviderGateOptions(global_limit=1,
        minimum_available_memory_fraction=None, memory_launch_interval_seconds=.001))
    gate = ProviderCallGate(tmp_path/"operational/llm", options.gate)
    held = gate.acquire('fixture',checkpoint=lambda:None)
    timer = threading.Timer(.35, held.release)
    timer.start()
    started = time.monotonic()
    try:
        with gate.acquire('fixture',checkpoint=lambda:None):
            result = ProcessRunner().run([sys.executable,'-c','print("ok")'],stdin=b'',env=None,cwd=tmp_path,
                idle_timeout_seconds=options.limits.idle_timeout_seconds,
                total_timeout_seconds=options.limits.total_timeout_seconds,stop_check=lambda:None)
        assert result.returncode == 0
        assert time.monotonic()-started >= .35
    finally:
        timer.join()


@pytest.mark.parametrize("headers", [True, False])
def test_http_stream_activity_cannot_extend_total_deadline(headers):
    import socket
    import threading
    import httpx
    from ac_llm.providers.http_api import _total_deadline, _trace_network_stream
    listener = socket.socket()
    listener.bind(('127.0.0.1',0))
    listener.listen()
    def serve():
        try:
            conn,_ = listener.accept()
            with conn:
                conn.recv(65536)
                if not headers:
                    time.sleep(.5)
                    return
                conn.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n')
                for _ in range(300):
                    conn.sendall(b'1\r\nx\r\n');time.sleep(.01)
        except OSError:
            pass
    thread = threading.Thread(target=serve,daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with httpx.Client(timeout=2,trust_env=False) as client:
            with pytest.raises(ProviderFailure) as exc:
                with _total_deadline(client,.15) as (active, check):
                    with client.stream('GET',f'http://127.0.0.1:{listener.getsockname()[1]}',
                        extensions={"trace": lambda event, info: _trace_network_stream(active, event, info)}) as response:
                        active[0] = response
                        for _ in response.iter_bytes(): check()
            assert exc.value.details['code'] == 'provider_total_timeout'
            assert time.monotonic()-started < 2
    finally:
        listener.close();thread.join(4)
