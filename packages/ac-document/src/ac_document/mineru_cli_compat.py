"""MinerU 3.4.5 console-entry compatibility, run by its own Python runtime."""

from functools import wraps
import importlib.metadata
from pathlib import Path
import runpy
import sys

import httpx


class _StatusClient:
    def __init__(self, client):
        self.client = client

    async def get(self, *args, **kwargs):
        try:
            return await self.client.get(*args, **kwargs)
        except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
            print(f'MinerU status connection interrupted ({type(exc).__name__}); retrying within the task deadline.', file=sys.stderr, flush=True)
            # MinerU already bounds and retries ReadTimeout in its polling loop.
            raise httpx.ReadTimeout(str(exc) or type(exc).__name__) from exc

    def __getattr__(self, name):
        return getattr(self.client, name)


def install_poll_retry(api_client):
    original = api_client.wait_for_task_result

    @wraps(original)
    async def wait_for_task_result(*args, **kwargs):
        if args:
            args = (_StatusClient(args[0]), *args[1:])
        else:
            kwargs['client'] = _StatusClient(kwargs['client'])
        return await original(*args, **kwargs)

    api_client.wait_for_task_result = wait_for_task_result


def main():
    executable = sys.argv[1]
    # Restore the console entry's import path; this package also has mineru.py.
    sys.path[0] = str(Path(executable).absolute().parent)
    if importlib.metadata.version('mineru') != '3.4.5':
        raise RuntimeError('The MinerU polling compatibility entry requires version 3.4.5.')
    from mineru.cli import api_client
    install_poll_retry(api_client)
    sys.argv = sys.argv[1:]
    runpy.run_path(executable, run_name='__main__')


if __name__ == '__main__':
    main()
