# ac-llm

`ac-llm` owns reusable host-LLM execution for AC Foundation: immutable requests,
provider and model resolution, structured-output validation, sessions, and
durable recovery over `ac-jobs`. Research-specific prompts and orchestration
belong to the packages that call it.

`codex_model_catalog()` performs a bounded, read-only Codex app-server
`model/list` request. It returns only visible model IDs, presentation metadata,
model-specific reasoning efforts, and the provider default. Invalid output,
timeouts, missing CLI support, or early process exit produce an unavailable
catalog without exposing stderr or changing the user's Codex configuration.

## Quick start

Installing the Python distribution exposes the `ac-llm` console script. AC
Foundation has no agent-host plugin or Skill; product launchers may install and
invoke this command inside their private runtime.

Run `ac-llm` outside a sandbox when possible; sandbox restrictions can cause
provider subprocesses to fail with permission errors.
Callers may set `ExecutionLimits.idle_timeout_seconds` to bound provider pipe
inactivity without imposing a total task deadline. Timeout cleanup terminates
the provider process group and preserves the typed timeout even on constrained
macOS hosts that permit signaling an owned group but deny signal-0 inspection.

Create `local/example/request.json` from the public v4 request contract, then
run one typed request in an explicit durable root:

```bash
ac-llm generate \
  --request local/example/request.json \
  --run-root local/example/.ac/llm \
  --host-authority <host-authority>
```

Persist `run.id`. Read the lifecycle at `data.run.status`; on success, the
verified result path is `data.run.result.path`, with a matching item in
`artifacts[]` whose `role` is `result`. Resolve only that returned path under
`<run-root>/runs/<run.id>/`; the model result is not inline in the command
envelope. Use `ac-llm --help` and
`ac-llm <command> --help` for current commands and flags, not the JSON request
contract.

Provider admission pauses by default when effective available system or
container memory falls below 10%. On Linux, host availability uses
`MemAvailable`; cgroup availability also counts inactive file cache reported
by `memory.stat`, because the kernel can reclaim it under pressure. Missing
cgroup statistics fall back to conservative raw headroom. Override the
threshold with `--minimum-available-memory-percent PERCENT`, or bypass the
check explicitly with `--disable-memory-guard`. The same flags are available
for `resume`.

AC Foundation's default **max parallel** provider target is 100 concurrent calls. This
is an admission target, not a hard ceiling: callers may set any positive
`ProviderGateOptions.global_limit`, and a caller-owned worker pool must create
the demand. The memory guard, provider-specific limits, and circuit breaker
can reduce effective concurrency below the target.
The gate and provider circuit are scoped to one explicit `RunRepository.root`.
Independent Companion project roots therefore do not open or close each
other's circuits, although the external provider or host may enforce its own
separate capacity limits.

When that caller uses an `ac-jobs` work group, its pending-work demand may be
changed live with `ac-jobs workers set`. This
changes the work-group target only. It does not raise or bypass the independent
provider gate, memory guard, or circuit breaker.

The Codex adapter projects response schemas into the provider-supported
Structured Outputs subset. Nested `oneOf` unions become `anyOf` only for the
native provider request; accepted output is still validated against the
original durable `oneOf` contract before it can be published.

`ModelSelection.reasoning_effort` is an optional semantic requirement separate
from model-routing `tier`. For the Codex provider, supported values are `low`,
`medium`, `high`, and `xhigh`; the adapter passes the selected value as an
explicit per-invocation `model_reasoning_effort` override on both start and
resume. When omitted, existing provider configuration behavior is preserved.

Set `<host-authority>` once per run: use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value when resuming that run.

For brokered host turns, `request_id` is task-scoped and at-most-once. Never
retry a host operation with the same ID: use a new ID for every new request,
including after a refusal. `ac-llm` may replay a durably recorded continuation
once to repair a provider that repeats an identical request, but it never calls
the host broker twice for that ID.

Before calling a broker, `ac-llm` persistently records the invocation. If the
process stops after that marker but before a broker response is durable,
resumption pauses with `host_broker_reconciliation_required`; submit a confirmed
`HostResponse` through the normal resume contract. It will not repeat the
possibly side-effecting broker call automatically.

## Python API

### Local applications and explicit API providers

`ModelSelection.reasoning_effort` is independent of the legacy model-selection
tier. The v4 request contract includes optional effort; local development v5
requests remain readable. Requests without effort keep their original encoding
and semantic identity. Consumers that persist model
selection must include the effort in their own versioned generation recipes.

The `LOCAL_APP` execution profile materializes verified inputs for tool-free
CLI calls and preserves bounded host-broker requests where the workflow needs
them. Codex uses a read-only sandbox; Claude disables tools, discovered settings
and MCP configuration. These are version-sensitive CLI contracts, not a claim
that arbitrary provider binaries are OS-isolated. They do not attest that the
host has unrestricted authority. Existing profiles keep their prior behavior.

`LOCAL_APP` admission defaults to the owning durable run's
`operational/llm` directory. Processes working on that run share its maximum of
eight provider attempts; independent runs, even in one project, each have their
own allowance. Lower global/provider limits are preserved. An explicit
`ProviderGateOptions.shared_root` is respected when a caller deliberately wants
a shared scope. `AC_HOME` does not pool unrelated document tasks.

Local-app attempts default to at most 300 seconds idle and 600 seconds total via
`ExecutionLimits.total_timeout_seconds`; callers may choose tighter limits.
Admission queue time is excluded. CLI process activity, including stderr, cannot
extend the total deadline. HTTP connections are interrupted before response
headers and during streaming when the total deadline expires. Exception details
identify `provider_idle_timeout` versus `provider_total_timeout`. Blocking
operating-system name resolution remains outside Python socket cancellation.
These are per-provider-attempt limits, not a whole-document deadline: multiple
batches, existing bounded repair/retry, and new host-request continuations can
run longer. This policy adds no retries and does not cap host-request rounds.
Limits are operational diagnostics, not part of semantic content identity.


The optional `api` extra provides direct Responses, Chat Completions-compatible
and Anthropic Messages adapters. `HTTPAPIAdapter` accepts a runtime credential
resolver; it does not persist credentials, follow redirects, or silently retry
requests that might have been billed. Structured JSON is prompt-constrained
and validated by the common output contract. Model-specific effort and image
support must be declared in `HTTPProviderConfig`; declarations are not probes.

CLIs can load an explicitly selected `AC_LLM_PROVIDER_CONFIG` JSON file:

```json
{
  "schema_version": "ac.llm.providers.v1",
  "providers": [{
    "name": "api-custom",
    "protocol": "responses",
    "base_url": "https://api.example.com/v1",
    "credential": {"kind": "environment", "name": "EXAMPLE_API_KEY"}
  }]
}
```

Pass an explicit model when using a custom provider. Configuration accepts only
secret references. The endpoint and declared capabilities bind adapter execution
identity, so an incompatible configuration cannot resume an existing execution.
`ProviderGateOptions.shared_root` lets an application share admission across its
jobs; callers sharing a root must use consistent limits.

Each actual provider attempt emits `llm_call_started`; terminal usage emits
`llm_usage` with a run/attempt/generation/host-turn call ID. Usage availability,
cache accounting and reasoning inclusion remain explicit. `PriceRates` computes
an estimate only when required categories are known, never a billed amount.
Missing terminal usage after interruption remains unknown. API transport timeout
or cancellation does not prove that a remote request was not billed.

`ac_llm.reference_pricing.api_reference_cost(model, usage)` separately computes
a dated standard/global API list-price equivalent for CLI or subscription use.
It never represents account charges. The catalog records official source URLs
and a verification date, requires a known concrete model, and prices each
request separately for supported long-context thresholds. Unknown cache-inclusion
or write-TTL semantics produce a range. Unreported cache writes, tools, regional
uplifts and service-tier fees are outside that reference; missing model prices
or input/output usage remain unavailable. Existing `PriceRates` behavior for
explicitly configured API prices stays strict.

Use `LLMClient` for a standalone durable task:

```python
from pathlib import Path

from ac_llm import LLMClient, LLMRequest, TextOutput

request = LLMRequest("summary-1", "Summarize the argument.", TextOutput())
result = LLMClient().generate(request, run_root=Path("local/example/.ac/llm"))
```

Package workflows that already have an `ac_jobs.RunContext` should use
`LLMTaskService` instead of creating a nested standalone run.

## Tests

The normal suite is offline; real-provider checks are opt-in:

```bash
python -m pytest packages/ac-llm/tests
```

On POSIX, a denied signal-0 process-group liveness probe does not abort stop
cleanup: the runner still attempts bounded group termination and reaps the child.
Actual signal-delivery failures remain errors.

Local-app CLI execution rejects API key and endpoint environment overrides before
starting or resuming a call. Configure custom services through an explicit API
connection; standard CLI execution retains its existing environment behavior.
