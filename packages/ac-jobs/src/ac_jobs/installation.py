"""Durable, caller-owned installation attempts with inherited kernel leases."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .errors import CorruptStateError, IdempotencyConflictError, RunBusyError
from .identity import canonical_json_bytes
from .storage import atomic_write_json


class InstallationOwnershipError(RuntimeError):
    """Exclusive ownership cannot be established on this filesystem."""


class InstallationRetryRequiredError(RuntimeError):
    """A retained incomplete operation requires an explicit retry choice."""


class InstallationCommandError(RuntimeError):
    """A supervised command did not provide a successful terminal receipt."""

    def __init__(self, message: str, record: Mapping[str, Any]):
        super().__init__(message)
        self.record = dict(record)


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise ValueError("not an object")
        return result
    except (OSError, ValueError) as exc:
        raise CorruptStateError(f"Unreadable installation state: {path}") from exc


def _probe(path: Path) -> dict[str, Any]:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return {"state": "unverifiable", "reason": "lease_is_not_regular_file"}
    try:
        import fcntl
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return {"state": "available"}
    except (ImportError, OSError) as exc:
        return {"state": "unverifiable", "error_type": type(exc).__name__}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "occupied"}
        except OSError as exc:
            return {"state": "unverifiable", "errno": exc.errno}
        return {"state": "available"}
    finally:
        os.close(fd)


class InstallationOperation:
    """One source-bound operation; status reads never initialize or install.

    The caller owns payload validation/publication. Commands run in a supervisor
    that preserves logs/receipts if only the coordinator is killed. No process
    is detached from platform execution authority. A held lease always wins
    over a terminal record; no PID, hostname or TTL implies safe takeover.
    """

    def __init__(self, directory: str | Path, identity: Mapping[str, Any]):
        self.directory = Path(directory).expanduser().resolve()
        self.identity = json.loads(canonical_json_bytes(dict(identity)))
        self.operation_id = "install-" + hashlib.sha256(canonical_json_bytes(self.identity)).hexdigest()
        self.fd: int | None = None
        self.attempt: Path | None = None
        self._state: dict[str, Any] = {}

    def _current(self) -> dict[str, Any]:
        path = self.directory / "state.json"
        saved = _read(path)
        if path.exists() and not saved:
            raise CorruptStateError(f"Unreadable installation state: empty metadata at {path}")
        if saved and (saved.get("operation_id") != self.operation_id or saved.get("identity") != self.identity):
            raise IdempotencyConflictError("Installation source/destination changed; choose a new operation destination and preserve existing state.")
        if saved and (saved.get("schema_version") != "ac.installation_attempt.v1"
                      or not isinstance(saved.get("attempt_id"), str)
                      or len(saved["attempt_id"]) != 32
                      or any(c not in "0123456789abcdef" for c in saved["attempt_id"])
                      or saved.get("state") not in {"running", "succeeded", "failed", "interrupted"}):
            raise CorruptStateError("Invalid installation attempt metadata")
        return saved

    def status(self) -> dict[str, Any]:
        lease = _probe(self.directory / "lease")
        result: dict[str, Any] = {"schema_version": "ac.installation_status.v1", "operation_id": self.operation_id,
                                 "directory": str(self.directory), "lease": lease}
        try:
            current = self._current()
            state = current.get("state", "not_started")
            if lease["state"] == "occupied":
                state = "running"
            elif lease["state"] == "unverifiable":
                state = "unverifiable"
            elif state == "running":
                state = "interrupted"
            commands = []
            if current.get("attempt_id"):
                commands = [_read(p) for p in sorted((self.directory / "attempts" / current["attempt_id"] / "commands").glob("*/result.json"))]
            events = []
            if current.get("attempt_id"):
                event_paths = sorted((self.directory / "attempts" / current["attempt_id"] / "events").glob("*.json"))[-24:]
                events = [_read(p) for p in event_paths]
            result.update(state=state, current=current, commands=commands, events=events)
        except (CorruptStateError, IdempotencyConflictError) as exc:
            result.update(state="unverifiable", error_type=type(exc).__name__, message=str(exc))
        return result

    def _save(self, **updates: Any) -> None:
        self._state.update(updates, updated_at=time.time())
        assert self.attempt is not None
        atomic_write_json(self.attempt / "state.json", self._state)
        atomic_write_json(self.directory / "state.json", self._state)

    @contextmanager
    def begin(self, *, retry: bool = False) -> Iterator[InstallationOperation]:
        if self.fd is not None:
            raise RuntimeError("Installation operation already holds a lease")
        try:
            import fcntl
        except ImportError as exc:
            raise InstallationOwnershipError("Installation requires POSIX flock support") from exc
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if (self.directory / "lease").is_symlink():
                raise InstallationOwnershipError("Installation lease must be a permanent regular file")
            self.fd = os.open(self.directory / "lease", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunBusyError("Installation lease is occupied; query status and wait for the existing operation") from exc
            except OSError as exc:
                raise InstallationOwnershipError(f"Installation lease cannot be established: {exc}") from exc
            previous = self._current()
            if previous and previous.get("state") != "succeeded" and not retry:
                raise InstallationRetryRequiredError("Previous installation attempt is incomplete; inspect status and explicitly retry")
            attempt_id = uuid.uuid4().hex
            self.attempt = self.directory / "attempts" / attempt_id
            self.attempt.mkdir(parents=True, mode=0o700)
            self._state = {"schema_version": "ac.installation_attempt.v1", "operation_id": self.operation_id,
                           "identity": copy.deepcopy(self.identity), "attempt_id": attempt_id, "state": "running",
                           "phase": "starting", "started_at": time.time(), "coordinator_pid": os.getpid(),
                           "previous_attempt": previous.get("attempt_id")}
            self._save()
            try:
                yield self
            except BaseException as exc:
                if self._state["state"] != "succeeded":
                    self._save(state="failed" if isinstance(exc, Exception) else "interrupted",
                               error_type=type(exc).__name__, message=str(exc), cause="execution outcome; cancellation intent unknown")
                raise
            else:
                if self._state["state"] != "succeeded":
                    self._save(state="interrupted", cause="coordinator exited without a completion receipt")
        finally:
            # Do not LOCK_UN: an inherited supervisor/command may still be writing.
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            self.attempt = None

    def checkpoint(self, phase: str, **details: Any) -> None:
        if self.fd is None or self.attempt is None or self._state.get("state") != "running":
            raise RuntimeError("Checkpoint requires an active installation attempt")
        event = {"schema_version": "ac.installation_event.v1", "phase": phase, "time": time.time(), "details": dict(details)}
        atomic_write_json(self.attempt / "events" / f"{time.time_ns()}-{uuid.uuid4().hex}.json", event)
        self._save(phase=phase, details=dict(details))

    def complete(self, result: Mapping[str, Any]) -> None:
        if self.fd is None or self.attempt is None:
            raise RuntimeError("Completion requires exclusive installation ownership")
        if self._state.get("state") == "succeeded":
            if self._state.get("result") != dict(result):
                raise IdempotencyConflictError("Installation completion result already committed")
            return
        self._save(state="succeeded", phase="completed", result=dict(result), completed_at=time.time())

    def run(self, command: list[str], *, env: Mapping[str, str] | None = None, timeout: float = 600) -> dict[str, Any]:
        if self.fd is None or self.attempt is None or self._state.get("state") != "running":
            raise RuntimeError("Command requires an active installation attempt")
        if timeout <= 0 or not math.isfinite(timeout) or not all(isinstance(v, str) for v in command) or not command:
            raise ValueError("Command and positive timeout are required")
        directory = self.attempt / "commands" / f"{time.time_ns()}-{uuid.uuid4().hex}"
        directory.mkdir(parents=True, mode=0o700)
        self._save(command_directory=str(directory))
        worker = Path(__file__).with_name("installation_worker.py")
        supervisor_log = directory / "supervisor.log"
        with supervisor_log.open("w", encoding="utf-8") as log:
            os.chmod(supervisor_log, 0o600)
            process = subprocess.Popen([sys.executable, str(worker), str(directory), str(self.fd)], stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=log,
                                       env=None if env is None else dict(env), pass_fds=(self.fd,))
            assert process.stdin is not None
            process.stdin.write(json.dumps({"command": command, "timeout": timeout}).encode())
            process.stdin.close()
            code = process.wait()
        record = _read(directory / "result.json")
        if not record or record.get("state") == "running":
            record.update(state="outcome_unknown", supervisor_returncode=code, supervisor_log=str(supervisor_log))
            atomic_write_json(directory / "result.json", record)
        if record.get("state") != "completed" or record.get("returncode") != 0 or not record.get("streams_complete"):
            raise InstallationCommandError(f"Installation command {record.get('state', 'outcome_unknown')}; inspect {directory}", record)
        def output(name):
            path = directory / (name + ".log")
            if path.stat().st_size > 8 * 1024 * 1024:
                raise InstallationCommandError(f"Command output exceeds capture limit; inspect {path}", record)
            return path.read_text(encoding="utf-8", errors="replace")
        return {"stdout": output("stdout"), "stderr": output("stderr"), "record": record, "directory": str(directory)}
