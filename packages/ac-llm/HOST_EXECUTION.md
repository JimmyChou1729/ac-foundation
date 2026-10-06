# Portable Host model execution

`host` exports a durable model task and returns promptly. It does not call the
current chat model from Python, poll a bridge, launch a model CLI, or acquire a
provider-call permit. The calling agent reads the task, completes it or delegates
it, submits a response, and resumes the owning workflow. The executor retains
ownership of output validation, acceptance, session prefixes, and retries.

## Configure a real coordinator

Configure this declaration only after checking the agent's actual tools:

```bash
export AC_LLM_HOST_COORDINATOR='{"coordinator_id":"my-agent","default_provider":"host","native_fallback":true,"fresh_context":true,"model_selection":false}'
```

Python callers can pass `HostCoordinator(...)` as
`LLMExecutionOptions(host_coordinator=...)`. `fresh_context` attests that the
coordinator can create independent agent contexts; shared filesystem access is
not OS isolation. `model_selection` describes available host controls, not an
API supplied by AC Foundation. No declaration grants tools or permissions.

For Codex, Claude Code, or Kimi Code, set `default_provider` to `codex`, `claude`,
or `kimi`. Native execution remains preferred. With `native_fallback=true`,
confirmed missing executables may select Host before the first provider launch.
Existing but unusable executables, authentication, quota, permission refusals,
user stops, and execution failures do not trigger provider switching. A native
session with accepted turns cannot automatically migrate to Host. Work,
Cowork, dot, and other hosts may declare `default_provider=host` directly,
even when a model CLI happens to be installed. Environment detection without a
coordinator declaration cannot enable Host. Explicit `model.provider=host`
always requests Host; `native_fallback=false` enforces the native route.

## Public coordination loop

1. Run the original command. Inspect its typed pause and owning run identity.
2. List outstanding tasks:
   `ac-llm host-pending --run-root <root> --run-id <run>`.
3. Export one task, optionally materializing verified inputs:

   ```bash
   ac-llm host-export --run-root <root> --run-id <run> \
     --task-id <host-task-id> --output-directory <export-directory>
   ```

4. Read `task.json`, `host/control.json`, and the materialized inputs. Accepted
   session history contains original prompts, results, and inputs; host-turn
   history includes durable broker files. History files live under
   `history/session/<ordinal>/` or `history/host/<round>/`, separately from the
   current task's files. Complete `response.template.json` according to the
   exported `response_schema` and original output contract.
5. Submit with `ac-llm host-submit --run-root <root> --run-id <run>
   --response <response-file>`.
6. Resume the **owning** workflow. For a standalone LLM run, use
   `ac-llm resume --run-root <root> --run-id <run>`. For a batch, use its public
   resume operation or product runner. Repeat until complete or truly blocked.

`LLMPaused.details.code=awaiting_host` uses the existing paused run lifecycle.
`input_required=false` means the owning resume command needs no supervisory
`ResumeInput`; it does not mean the model response is optional. The exported
task supplies `response_contract=ac.llm.host_response.v1` and its full schema.
Coordinators never edit state JSON or read private run layouts.

The stable task ID binds run, logical task, recovery epoch, generation,
host-turn ordinal, complete input digest, and duplicate-recovery phase. Moving
the run directory does not change it. Submissions reject wrong IDs/digests,
duplicate JSON keys, non-finite numbers, malformed JSON, and schema violations.
The same normalized response is idempotent, including after completion;
conflicting responses are rejected. Submit persists a receipt before resume
consumes it through the original CandidateMaterial and acceptance path.

Independent proposer/reviewer workers require separate context IDs. A worker
may continue its own context across host turns and rounds. Different workers
in the same loop/scope cannot share a context ID, including concurrent submits.
Without the required fresh-context capability, the task remains unavailable;
the coordinator must not imitate independent review by changing roles in one
conversation. Fake actors are for deterministic offline fixtures.

## Model and provenance

Without a model preference, the agent inherits its host model and effort.
Requested model/tier/effort remain visible in the task package. If a requested
model cannot be selected, the coordinator may use its host model under the
documented `use_host_model` policy and report the substitution. Set
`actual_model` and `reasoning_effort` to null when the host cannot attest them;
unknown usage is null. Do not invent token counts, cost, model identity, or
isolation. `host-pending --all` exposes actor, requested/actual model, route,
and consumed/completed status; original raw provider material retains detailed
provenance. Host workflow recovery is supported; native internal model-session
resumption is not claimed.

## Stop, interruption, and compatibility

Stopping a paused run records the existing attempt-scoped stop token and rejects
new host submissions. Matching already submitted responses remain idempotent.
Only an explicitly requested workflow resume begins a new attempt. Coordinators
must inspect stop requests and user steering before automatically resuming.
Authentication or policy refusal is a real blocker, not a fallback signal.

The existing LLM task/session state schemas are unchanged. New Host receipts use
their own versioned contract. Interrupted export, submit, raw publication, and
session acceptance recover without new model generations or accepted-prefix
duplication. Formatter tasks follow the selected provider and are exported as
ordinary child tasks. Brokered output must submit the complete
`ac.llm.host_turn.v1` envelope, preserving tool permission boundaries.

Offline tests use fake actors and actual subprocess termination. Real Work,
Cowork, Kimi, and dot tool availability and model controls require separate
host smoke tests. Copying a durable directory is tested; automatic transfer
between machines, host account access, and platform installation are separate
integration concerns. No external paid model API is required by this provider.

Workflow adapters can use `LLMClient.run_id_for(request)` before calling
`generate` to inspect a previous run without starting an attempt. An adapter
that automatically retries should preserve an existing stop request or a pause
requiring supervisory input; only an explicit resume clears that stop boundary.
