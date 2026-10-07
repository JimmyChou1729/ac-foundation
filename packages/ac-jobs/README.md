# ac-jobs

`ac-jobs` is AC Foundation's zero-dependency durable-run kernel. It owns atomic run
state, immutable artifacts, cooperative stopping, pause/resume, explicit failed-run
recovery, work groups, and the shared command-result codec. It does not own
provider selection, detached processes, or research-domain workflows.

## Quick start

Installing the Python distribution exposes the `ac-jobs` console script. AC
Foundation has no agent-host plugin or Skill; product launchers may install and
invoke this command inside their private runtime.

Inspect a run whose owning workflow exposed a generic root and ID. For a
direct-root owner such as `ac-llm`, preserve the exact `--run-root` input and
pair it with the returned `run.id`:

```bash
ac-jobs status --run-root local/example/runs --run-id run-001
```

The command envelope is a query: read the durable lifecycle at
`data.run.status`, not only top-level `status`. The result artifact ID and
returned path are at `data.run.result.artifact_id` and
`data.run.result.path`; failures and pauses are at `data.run.error` and
`data.run.resume`; exact editable recovery paths are under
`data.run.working_state`. `validate` returns `data.valid` and `data.issues[]`,
while `stop` returns `data.run.stop_requested` and `data.run.status`.

`stop` also records an attempt-scoped request while a run is paused. This
prevents late external work from being submitted to that attempt. Repeated
stops preserve the first request; an explicit owning-workflow resume starts a
new attempt. Completed and failed runs remain unchanged.

Work groups expose a durable runtime concurrency target. Owners choose the
initial target through `max_workers`; an operator may inspect or change it
without stopping the run:

```bash
ac-jobs workers get \
  --run-root local/example/runs --run-id run-001 --group-id summaries
ac-jobs workers set \
  --run-root local/example/runs --run-id run-001 --group-id summaries \
  --workers 100
```

Increasing the target admits pending units immediately. Decreasing it never
cancels in-flight work; new units wait until the in-flight count falls below
the new target. The target is operational state, not semantic input, and
survives pause, process replacement, and failed-run recovery. Provider gates,
memory admission, and circuit breakers may keep effective concurrency below
the requested target. Only groups that have started have worker controls.

`ac-jobs` deliberately has no resume command. Resume through the package that
created the run, using the same run root and ID.
Use `ac-jobs --help` and `ac-jobs <command> --help` for current flags.

## Python API

The public repository API can inspect the same durable run:

```python
from ac_jobs import RunRepository

view = RunRepository("local/example/runs").inspect("run-001")
print(view.snapshot.status.value)

workers = RunRepository("local/example/runs").set_group_workers(
    "run-001", "summaries", 100
)
print(workers.target_workers)
```

Higher-level packages create and resume their own runs; use their public
handlers or facades instead of constructing package-internal run specs.
Project-based packages normally provide project-aware status, validation, and
stop commands; use those rather than deriving their internal job roots. There
is no generic fixture-creation CLI because an ownerless run has neither valid
package semantics nor a package resume path.

`failed` records the latest failed execution attempt; it is not a permanent
project terminal state. Only an explicit `RunEngine.resume` may retry it.
That retry increments `recovery_epoch`, snapshots the editable `working/`
tree, gives LLM tasks and work groups a fresh execution namespace, reuses
matching successful group units, and retries failed units. `execute` never
forms an automatic recovery loop, and a succeeded run remains final.

Each run exposes `working/semantic-input.json`, `working/artifacts/`,
`working/candidates/`, `working/index.json`, and `working/last-error.json`.
An agent may edit a readable file to adopt its current bytes or delete an
artifact/candidate to regenerate it. Recovery rehashes current files and emits
`working_state_modified`; if semantic input changed while downstream files
remain it also emits a broad stale-state warning. Immutable specs, old
fingerprints, object-store content, recovery snapshots, and locks are not
edited.

`RunEngine.execute` and `RunEngine.resume` accept an optional runtime-only
`event_sink`. The sink receives each newly fsynced `ac.jobs.event.v1`
document and never receives historical replay. Sink failures are isolated from
the durable run and recorded best-effort as `progress_sink_failed` events.
Progress event data may contain any valid JSON body; individual durable events
remain limited to 256 KiB.

## Explicit installation operations

`InstallationOperation` is a separate, POSIX-only lifecycle for caller-owned
environment installation. It does not alter research-run stopping or provider
selection. The caller chooses a stable operation directory from the payload
destination and supplies its immutable source identity:

```python
from ac_jobs import InstallationOperation

operation = InstallationOperation("local/.tools.ac-install", {
    "destination": "local/tools", "source_sha256": "<locked-source-digest>"
})
print(operation.status())  # Read-only; does not even create the directory.
with operation.begin(retry=False):
    operation.checkpoint("installing")
    execution = operation.run(["<installer>", "<arguments>"], timeout=600)
    # Validate and atomically publish a fresh attempt-owned payload here.
    operation.complete({"payload_checked": True})
```

`begin` acquires a permanent kernel `flock` lease without waiting. An occupied
lease raises `RunBusyError`; unsupported/unverifiable ownership raises
`InstallationOwnershipError`. A source mismatch or corrupt record fails
closed. A prior incomplete attempt requires an explicit `begin(retry=True)`
and raises `InstallationRetryRequiredError` otherwise. Neither PID existence,
hostname nor elapsed time authorizes takeover. Never unlink the lease file or
use source-specific operation directories to bypass an occupied destination.
Shared filesystems must provide coherent `flock` semantics; this API does not
certify a mount's locking behavior.

Each attempt retains phase events, state and command stdout/stderr logs.
A supervisor and its command inherit the lease. Closing the coordinator's
descriptor does not unlock surviving children. Commands remain under the
same process-group/platform authority; this is not a detached service or an
approval bypass. Killing only the coordinator can leave a queryable supervisor;
killing the whole process group/container can leave only the last durable
phase. `status` reports `running` whenever the lease is held, even if a terminal
record exists. A running record with a free lease becomes `interrupted`.
Process IDs are diagnostic only; a cancellation's intent is not inferred.

A successful command requires an exit-zero terminal receipt and complete
logs. Failures, timeouts, incomplete streams or missing receipts raise
`InstallationCommandError` with the saved record. Commands and environment
values are not persisted; URL userinfo in command output is redacted. Callers
must still avoid printing credentials. Full logs are retained; returned output
is limited to 8 MiB per stream. Only the caller can validate payloads and call
`complete`; command success alone is not installation success. Caller recovery
should preserve uncertain trees and build a new attempt from verified inputs,
then reconcile a valid published payload if publication preceded a crash.

## Tests

From the repository root:

```bash
python -m pytest packages/ac-jobs/tests
```

`RunContext.run_group(..., continue_after_pause=predicate)` can keep admitting
independent units after a caller-classified local pause. The default remains to
stop admission on any pause. Shared pauses take precedence in the returned
outcome; paused units are not persisted as completed, while successful neighbors
remain reusable on resume. The predicate must not classify missing authority or
shared-service failures as local content failures.
