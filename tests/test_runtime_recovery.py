"""Process-level public setup/retry tests; fake uv never contacts a provider."""
from __future__ import annotations

import json
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("ac_recovery_runtime", Path(__file__).resolve().parents[1] / "runtime/ac_runtime.py")
RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNTIME
SPEC.loader.exec_module(RUNTIME)


def _lock():
    return {"schema_version": "ac.runtime_sources.v2", "profile": "recovery-test",
            "sources": [{"id": "foundation", "repository": "https://example.com/foundation.git",
                         "commit": "a" * 40, "packages": ["ac-jobs"], "tools": ["ac-jobs"],
                         "local_root_env": "AC_FOUNDATION_REPO_ROOT"}], "environment_defaults": {}}

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX flock recovery contract")

FAKE_UV = r'''
import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
if args[0]=='venv':
    root=Path(args[1]);(root/'bin').mkdir(parents=True)
    (root/'bin/python').symlink_to(sys.executable)
    sys.exit(0)
root=Path(args[args.index('--python')+1]).parent.parent
control=Path(os.environ['FIXTURE_ROOT'])
with (control/'calls.jsonl').open('a') as f:
    f.write(json.dumps({'pid':os.getpid(),'venv':str(root),'UV_CACHE_DIR':os.environ.get('UV_CACHE_DIR'),'PIP_CACHE_DIR':os.environ.get('PIP_CACHE_DIR'),'TMPDIR':os.environ.get('TMPDIR'),'HOME':os.environ.get('HOME'),'UV_PYTHON_DOWNLOADS':os.environ.get('UV_PYTHON_DOWNLOADS')})+'\n')
if os.environ.get('DROP_LOCK')=='1':
    for fd in range(3,256):
        try:os.close(fd)
        except OSError:pass
if os.environ.get('HOLD_INSTALL')=='1':
    (control/'child.json').write_text(json.dumps({'pid':os.getpid(),'venv':str(root)}))
    print('installer waiting',flush=True)
    while not (control/'release').exists():time.sleep(.02)
(root/'bin/ac-jobs').write_text('#!'+str(root/'bin/python')+'\nprint('+repr(os.environ.get('GENERATION','first'))+')\n')
(root/'bin/ac-jobs').chmod(0o755)
(root/'finished').touch()
'''


@pytest.fixture
def public_runtime(tmp_path, monkeypatch):
    source = tmp_path / "sources.json"
    source.write_text(json.dumps(_lock()))
    fake = tmp_path / "uv"
    fake.write_text(f"#!{sys.executable}\n" + FAKE_UV)
    fake.chmod(0o755)
    env = dict(os.environ)
    for name in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "UV_NO_CACHE", "PIP_NO_CACHE_DIR", "TMPDIR", "TEMP", "TMP", "AC_INSTALL_RETRY"):
        env.pop(name, None)
    env.update(AC_RUNTIME_SOURCES_FILE=str(source), AC_RUNTIME_CONSTRAINTS_FILE=str(tmp_path / "none"),
               AC_RUNTIME_HOME=str(tmp_path / "runtimes"), AC_INSTALL_SOURCE="git", AC_INSTALL_UV=str(fake),
               FIXTURE_ROOT=str(tmp_path), PYTHONDONTWRITEBYTECODE="1", AC_INSTALL_LOCK_TIMEOUT_SEC=".3")
    command = [sys.executable, str(RUNTIME.__file__)]
    doctor = subprocess.run([*command, "doctor"], env=env, capture_output=True, text=True)
    runtime = Path(json.loads(doctor.stdout)["runtime"])
    assert not runtime.exists(), "doctor must not create runtime/cache directories"
    return command, env, runtime, tmp_path


def run(public_runtime, *args, extra=None):
    command, env, _, _ = public_runtime
    return subprocess.run([*command, *args], env={**env, **(extra or {})}, capture_output=True, text=True, timeout=15)


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("fixture did not reach expected state")


def start_held(fixture, **extra):
    command, env, _, root = fixture
    process = subprocess.Popen([*command, "setup"], env={**env, "HOLD_INSTALL": "1", **extra},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    wait_for(lambda: (root / "child.json").is_file())
    attempt = Path(json.loads((root / "child.json").read_text())["venv"]).parent
    wait_for(lambda: "installer waiting" in (attempt / "install.log").read_text())
    return process


def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def test_private_caches_bypass_unwritable_default_and_setup_is_idempotent(public_runtime):
    _, env, runtime, root = public_runtime
    default = root / "readonly-cache"
    default.mkdir(mode=0o500)
    result = run(public_runtime, "setup", extra={"XDG_CACHE_HOME": str(default)})
    assert result.returncode == 0, result.stderr
    calls = (root / "calls.jsonl").read_text().splitlines()
    child = json.loads(calls[0])
    assert child['UV_CACHE_DIR'] == str(runtime / "cache/uv")
    assert child['PIP_CACHE_DIR'] == str(runtime / "cache/pip")
    assert child['TMPDIR'] == str(runtime / "tmp")
    assert child['HOME'] == env.get('HOME')
    assert child['UV_PYTHON_DOWNLOADS'] == 'never'
    assert not list(default.iterdir())
    assert run(public_runtime, "setup").returncode == 0
    assert (root / "calls.jsonl").read_text().splitlines() == calls
    d = run(public_runtime, "doctor")
    assert d.returncode == 0 and json.loads(d.stdout)['ready']
    assert json.loads(d.stdout)['lock']['probe']['state'] == 'available'
    assert run(public_runtime, "run", "ac-jobs").stdout.strip() == "first"


def test_explicit_cache_and_temp_paths_are_used(public_runtime):
    _, _, _, root = public_runtime
    extra = {key: str(root / key.lower()) for key in ("UV_CACHE_DIR", "PIP_CACHE_DIR", "TMPDIR")}
    assert run(public_runtime, "setup", extra=extra).returncode == 0
    call = json.loads((root / "calls.jsonl").read_text().splitlines()[0])
    assert all(call[key] == value for key, value in extra.items())


def test_explicit_unusable_path_preserves_failure_and_retry_evidence(public_runtime):
    _, _, runtime, root = public_runtime
    blocked = root / "not-a-directory"
    blocked.write_text("keep")
    result = run(public_runtime, "setup", extra={"UV_CACHE_DIR": str(blocked)})
    assert result.returncode == 1 and 'install_path_unwritable' in result.stderr
    failure = json.loads((runtime / "install.failed").read_text())
    assert Path(failure['log']).is_file()
    assert not run(public_runtime, "doctor").returncode == 0
    assert run(public_runtime, "setup").returncode == 1
    assert run(public_runtime, "setup", "--retry").returncode == 0
    assert Path(failure['attempt']).is_dir() and Path(failure['log']).read_text()
    assert blocked.read_text() == "keep"


def test_doctor_handles_empty_configuration_without_writes(public_runtime):
    _, _, runtime, _ = public_runtime
    result = run(public_runtime, "doctor", extra={"UV_CACHE_DIR": ""})
    assert result.returncode == 1
    assert json.loads(result.stdout)['path_configuration_error'] == 'UV_CACHE_DIR must not be empty'
    assert not runtime.exists()


def test_competing_installer_cannot_take_over_live_lock(public_runtime):
    _, _, runtime, root = public_runtime
    process = start_held(public_runtime)
    try:
        # Simulated foreign PID namespace/host, ancient metadata, and absent PID.
        owner_path = runtime / 'install.owner.json'
        owner = json.loads(owner_path.read_text())
        owner.update(host='foreign-host', pid=99999999, acquired_at=0, pid_namespace='foreign')
        owner_path.write_text(json.dumps(owner))
        result = run(public_runtime, "setup", "--retry", extra={"AC_INSTALL_LOCK_STALE_SEC": "0"})
        assert result.returncode == 75
        assert 'lock_occupied' in result.stderr and 'lock_wait_timeout' in result.stderr
        assert len((root / 'calls.jsonl').read_text().splitlines()) == 1
        assert json.loads(run(public_runtime, 'doctor').stdout)['lock']['probe']['state'] == 'occupied'
        (root / 'release').touch()
        assert process.wait(timeout=5) == 0
        assert run(public_runtime, 'setup', '--retry').returncode == 0
    finally:
        stop_group(process)


def test_hard_kill_retains_child_lock_then_public_retry_recovers(public_runtime):
    _, _, runtime, root = public_runtime
    process = start_held(public_runtime)
    try:
        process.kill(); process.wait(timeout=5)
        child = json.loads((root / 'child.json').read_text())
        assert run(public_runtime, 'setup', '--retry').returncode == 75
        assert not json.loads(run(public_runtime, 'doctor').stdout)['ready']
        os.kill(child['pid'], signal.SIGKILL)
        wait_for(lambda: RUNTIME._lock_diagnostic(runtime / 'install.lock')['state'] == 'available')
        result = run(public_runtime, 'setup', '--retry')
        assert result.returncode == 0, result.stderr
        assert 'lock_recovered' in result.stderr and 'install_attempt_abandoned' in result.stderr
        assert len(list((runtime / 'attempts').iterdir())) == 2
        old = Path(child['venv']).parent
        assert 'installer waiting' in (old / 'install.log').read_text()
    finally:
        stop_group(process)


def test_reused_unrelated_pid_does_not_block_recovery(public_runtime):
    _, _, runtime, _ = public_runtime
    runtime.mkdir(parents=True)
    (runtime / 'install.lock').touch()
    (runtime / 'install.owner.json').write_text(json.dumps({'state':'held','pid':os.getpid(),'host':RUNTIME.socket.gethostname(),'acquired_at':0}))
    result = run(public_runtime, 'setup', '--retry')
    assert result.returncode == 0 and 'lock_recovered' in result.stderr


def test_orphan_without_inherited_fd_cannot_write_published_attempt(public_runtime):
    _, _, runtime, root = public_runtime
    process = start_held(public_runtime, DROP_LOCK='1', GENERATION='old')
    try:
        process.kill(); process.wait(timeout=5)
        old = Path(json.loads((root / 'child.json').read_text())['venv'])
        result = run(public_runtime, 'setup', '--retry', extra={'GENERATION':'new'})
        assert result.returncode == 0, result.stderr
        published = (runtime / 'venv').readlink()
        assert published != old and old.is_dir()
        (root / 'release').touch()
        wait_for(lambda: (old / 'finished').exists())
        assert (runtime / 'venv').readlink() == published
        assert run(public_runtime, 'run', 'ac-jobs').stdout.strip() == 'new'
    finally:
        stop_group(process)


@pytest.mark.parametrize('marker', [[], None, {}, {'fingerprint':'test'}])
def test_partial_or_malformed_runtime_never_ready(tmp_path, marker):
    (tmp_path / 'install.ok').write_text(json.dumps(marker))
    (tmp_path / 'venv/bin').mkdir(parents=True)
    (tmp_path / 'venv/bin/ac-jobs').touch()
    assert not RUNTIME._ready(tmp_path, 'test', ('ac-jobs',))


def test_legacy_runtime_is_preserved_and_never_reclaimed(public_runtime):
    _, env, runtime, _ = public_runtime
    legacy = Path(env["AC_RUNTIME_HOME"]) / "v1/legacy/install.lock"
    legacy.mkdir(parents=True)
    (legacy / "owner.json").write_text("old evidence")
    assert "/v2/" in str(runtime)
    assert run(public_runtime, "setup", "--retry").returncode == 0
    assert (legacy / "owner.json").read_text() == "old evidence"


def test_unverifiable_filesystem_lock_fails_closed(tmp_path, monkeypatch, capsys):
    import errno
    import fcntl
    def unsupported(*args):
        raise OSError(errno.ENOTSUP, "unsupported lock filesystem")
    monkeypatch.setattr(fcntl, "flock", unsupported)
    with pytest.raises(SystemExit) as exc:
        with RUNTIME.InstallLock(tmp_path / "install.lock"):
            raise AssertionError("must not install without ownership")
    assert exc.value.code == 75
    assert 'lock_ownership_unverifiable' in capsys.readouterr().err


def test_real_uv_retains_lock_after_parent_is_killed(tmp_path):
    import http.server
    import shutil
    import threading
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("real uv not installed; fake-uv process tests remain active")
    arrived, release = threading.Event(), threading.Event()
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            arrived.set()
            release.wait(15)
            self.send_error(404)
        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    lock = tmp_path / 'install.lock'
    log = tmp_path / 'install.log'
    script = tmp_path / 'owner.py'
    script.write_text('''import importlib.util,sys,os
from pathlib import Path
spec=importlib.util.spec_from_file_location("runtime",sys.argv[1]);mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
with mod.InstallLock(Path(sys.argv[2])) as ownership:
    mod._run_logged([sys.argv[4],"pip","install","--target",sys.argv[5],"arc-nonexistent-recovery-fixture"],Path(sys.argv[3]),env=dict(os.environ),lock_fd=ownership.fd)
''')
    env = {**os.environ, 'UV_CACHE_DIR':str(tmp_path/'uv-cache'), 'UV_INDEX_URL':f'http://127.0.0.1:{server.server_port}/simple',
           'UV_HTTP_RETRIES':'0', 'NO_PROXY':'127.0.0.1', 'no_proxy':'127.0.0.1', 'UV_PYTHON_DOWNLOADS':'never'}
    process = subprocess.Popen([sys.executable,str(script),str(RUNTIME.__file__),str(lock),str(log),uv,str(tmp_path/'target')],
                               env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        assert arrived.wait(10), log.read_text() if log.exists() else 'uv did not reach the local fixture index'
        process.kill(); process.wait(timeout=5)
        assert RUNTIME._lock_diagnostic(lock)['state'] == 'occupied'
    finally:
        stop_group(process)
        release.set()
        server.shutdown(); server.server_close(); worker.join(timeout=5)
    wait_for(lambda: RUNTIME._lock_diagnostic(lock)['state'] == 'available')
