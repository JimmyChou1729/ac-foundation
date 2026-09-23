import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys

import httpx
import pytest

from ac_document.mineru_cli_compat import install_poll_retry
from ac_document.mineru_runner import _local_command


@pytest.mark.parametrize('error', [httpx.ReadError, httpx.RemoteProtocolError])
def test_status_disconnect_reuses_bounded_poll_loop(error):
    calls = []
    class Client:
        async def get(self, url):
            calls.append(url)
            if len(calls) == 1:
                raise error('')
            return 'completed'
        async def post(self, *args, **kwargs):
            raise AssertionError('Submission must not be retried')
    async def poll(client, response, label, *, timeout_seconds):
        assert timeout_seconds == 42
        for _ in range(2):
            try:
                return await client.get(response)
            except httpx.ReadTimeout:
                pass
        raise TimeoutError('bounded')
    api = SimpleNamespace(wait_for_task_result=poll)
    install_poll_retry(api)
    assert asyncio.run(api.wait_for_task_result(Client(), '/status/one', 'paper', timeout_seconds=42)) == 'completed'
    assert calls == ['/status/one', '/status/one']


def test_other_errors_and_existing_deadline_are_preserved():
    class Client:
        async def get(self, url):
            raise httpx.ConnectError('not a polling read interruption')
    async def poll(*, client, timeout_seconds):
        if timeout_seconds == 0:
            raise TimeoutError('deadline expired')
        return await client.get('/status')
    api = SimpleNamespace(wait_for_task_result=poll)
    install_poll_retry(api)
    with pytest.raises(TimeoutError, match='deadline expired'):
        asyncio.run(api.wait_for_task_result(client=Client(), timeout_seconds=0))
    with pytest.raises(httpx.ConnectError):
        asyncio.run(api.wait_for_task_result(client=Client(), timeout_seconds=10))


def test_only_recognized_console_entry_uses_its_own_python(tmp_path):
    entry = tmp_path / 'mineru'
    entry.write_text(f'#!{sys.executable}\nfrom mineru.cli.client import main\nmain()\n')
    command = _local_command(str(entry))
    assert command[0] == sys.executable
    assert Path(command[1]).name == 'mineru_cli_compat.py'
    assert command[2] == str(entry)
    entry.write_text('#!/bin/sh\nexec custom-mineru "$@"\n')
    assert _local_command(str(entry)) == [str(entry)]


def test_native_executable_is_left_untouched(tmp_path):
    entry = tmp_path / 'mineru-native'
    entry.write_bytes(b'\x7fELF' + b'\xff' * 16384)
    assert _local_command(str(entry)) == [str(entry)]
