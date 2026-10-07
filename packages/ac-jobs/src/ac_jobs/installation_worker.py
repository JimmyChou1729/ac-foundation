"""Internal supervised command process; not a detached/background service."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from ac_jobs.storage import atomic_write_json


def main():
    directory, lease_fd = Path(sys.argv[1]), int(sys.argv[2])
    spec = json.loads(sys.stdin.buffer.read())
    command = spec['command']
    record = {"schema_version": "ac.installation_command.v1", "state": "running", "started_at": time.time(),
              "supervisor_pid": os.getpid(), "executable": Path(command[0]).name}
    for name in ('stdout.log', 'stderr.log'):
        (directory / name).touch(mode=0o600)
    atomic_write_json(directory / 'result.json', record)
    threads = []
    stream_errors = []
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   errors='replace', pass_fds=(lease_fd,))
        record['command_pid'] = process.pid
        atomic_write_json(directory / 'result.json', record)
        def pump(stream, name):
            path = directory / (name + '.log')
            try:
                with path.open('w', encoding='utf-8') as log:
                    os.chmod(path, 0o600)
                    for line in stream:
                        line = re.sub(r'([a-z][a-z0-9+.-]*://)[^/@\s]+@', r'\1[REDACTED]@', line)
                        log.write(line); log.flush()
            except Exception as exc:
                stream_errors.append({"stream": name, "error_type": type(exc).__name__, "message": str(exc)})
            finally:
                stream.close()
        for stream, name in ((process.stdout,'stdout'),(process.stderr,'stderr')):
            thread = threading.Thread(target=pump,args=(stream,name),daemon=True)
            thread.start(); threads.append(thread)
        state = 'completed'
        try:
            code = process.wait(timeout=spec['timeout'])
        except subprocess.TimeoutExpired:
            process.kill(); code = process.wait(); state = 'timed_out'
        for thread in threads:
            thread.join(timeout=5)
        if any(t.is_alive() for t in threads) and state == 'completed':
            state = 'outcome_unknown'
        if stream_errors and state == 'completed':
            state = 'logging_failed'
        if code != 0 and state == 'completed':
            state = 'failed'
        record.update(state=state, returncode=code, stream_errors=stream_errors,
                      streams_complete=not any(t.is_alive() for t in threads), finished_at=time.time())
    except BaseException as exc:
        record.update(state='failed',error_type=type(exc).__name__,message=str(exc),finished_at=time.time())
    atomic_write_json(directory / 'result.json', record)
    return 0 if record['state']=='completed' and record.get('returncode')==0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
