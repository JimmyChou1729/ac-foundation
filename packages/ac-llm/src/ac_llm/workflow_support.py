"""Small policy-free adapters for package-owned durable LLM workflows."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from typing import Any, Protocol

from ac_jobs import Awaiting, RunContext, RunError, RunSnapshot, RunStatus, canonical_json_bytes

from .identity import resume_input_matches
from .outcome import LLMFailed, LLMPaused, LLMTaskOutcome
from .request import LLMExecutionOptions, LLMRequest, ResumeInput


class LLMTaskExecutor(Protocol):
    def execute_or_resume(
        self,
        context: RunContext,
        request: LLMRequest,
        *,
        input: ResumeInput | None = None,
        options: Any = ...,
    ) -> LLMTaskOutcome: ...


def execute_or_resume_matching(
    service: LLMTaskExecutor,
    context: RunContext,
    request: LLMRequest,
    *,
    resume_input: ResumeInput | None,
    options: LLMExecutionOptions,
) -> LLMTaskOutcome:
    """Resume only when the supplied input belongs to this exact request."""

    if resume_input is not None and resume_input_matches(request, resume_input):
        return service.execute_or_resume(
            context, request, input=resume_input, options=options
        )
    return service.execute_or_resume(context, request, options=options)


def awaiting_from_pause(outcome: LLMPaused) -> Awaiting:
    return Awaiting(
        outcome.reason,
        outcome.resume_key,
        outcome.input_required,
        outcome.request_ref,
        outcome.response_contract,
        outcome.details,
    )


def _host_resume_details(details, *, run_root, run_id):
    if not details.get("host_task_id") or "awaiting_host" not in (
        details.get("code"), details.get("llm_code")
    ):
        return details
    # An explicitly located child run is not owned by this snapshot.
    if details.get("run_id", run_id) != run_id:
        return details
    return {**details, "run_root": str(Path(run_root).expanduser().resolve()), "run_id": run_id}


def with_host_resume_location(snapshot: RunSnapshot, *, run_root: str | Path) -> RunSnapshot:
    """Present a Host pause with its current owning repository location.

    This also supports old pauses and relocated projects without changing their
    stored snapshots, request hashes, receipts, or recovery epochs. The caller
    must supply the repository that owns the snapshot, not a guessed project path.
    """
    if snapshot.status is not RunStatus.PAUSED or snapshot.awaiting is None:
        return snapshot
    details = _host_resume_details(
        snapshot.awaiting.details, run_root=run_root, run_id=snapshot.run_id
    )
    if details == snapshot.awaiting.details:
        return snapshot
    return replace(snapshot, awaiting=replace(snapshot.awaiting, details=details))


def run_error_from_failure(outcome: LLMFailed) -> RunError:
    return RunError(
        outcome.error.code.value,
        str(outcome.error),
        outcome.error.details,
    )


def semantic_retry_request(
    request: LLMRequest,
    *,
    identity_schema_version: str,
    validator_contract: str,
    feedback: str,
) -> LLMRequest:
    """Create one deterministic fresh task after semantic validation."""

    bounded_feedback = feedback.strip()[:4000]
    identity = {
        "schema_version": identity_schema_version,
        "source_task_id": request.task_id,
        "validator_contract": validator_contract,
        "feedback": bounded_feedback,
    }
    digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    task_id = f"{request.task_id[:72]}-semantic-retry-{digest[:24]}"
    prompt = "\n\n".join(
        (
            request.prompt,
            (
                "AC Foundation package validation found that the previous JSON response "
                "was structurally valid but unusable. Produce a complete fresh "
                "response for the original task; do not merely describe or patch "
                "the prior response."
            ),
            f"Validator contract: {validator_contract}",
            f"Validation feedback:\n{bounded_feedback}",
        )
    )
    return replace(request, task_id=task_id, prompt=prompt)


__all__ = [
    "LLMTaskExecutor",
    "awaiting_from_pause",
    "with_host_resume_location",
    "execute_or_resume_matching",
    "run_error_from_failure",
    "semantic_retry_request",
]
