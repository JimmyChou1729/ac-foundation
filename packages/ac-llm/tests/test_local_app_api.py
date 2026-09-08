from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from ac_jobs import StopToken
from ac_llm import (
    LLMRequest,
    ModelSelection,
    TextOutput,
    decode_request,
    request_to_document,
)
from ac_llm.identity import semantic_key
from ac_llm.providers.base import ProviderRequest, ProviderTerminalKind
from ac_llm.providers.http_api import HTTPAPIAdapter, HTTPProviderConfig
from ac_llm.usage import PriceRates


class Observer:
    def progress(self, kind, data):
        pass


def request(tmp_path, effort=None):
    (tmp_path / "host").mkdir(exist_ok=True)
    (tmp_path / "host/control.json").write_text(
        json.dumps(
            {
                "schema_version": "ac.llm.workspace_control.v1",
                "prompt": "Explain this short source.",
                "inputs": [],
                "output_contract": {"kind": "text"},
            }
        )
    )
    return ProviderRequest(
        "Read host/control.json",
        "fixture-model",
        None,
        {"reasoning_effort": effort},
        10,
        tmp_path,
    )


def test_effort_changes_identity_without_changing_legacy_encoding():
    original = LLMRequest(
        "one", "Read this", TextOutput(), ModelSelection("codex", "fixture-model")
    )
    assert request_to_document(original)["schema_version"] == "ac.llm.request.v4"
    extended = replace(original, model=replace(original.model, reasoning_effort="high"))
    assert request_to_document(extended)["schema_version"] == "ac.llm.request.v4"
    assert decode_request(request_to_document(extended)) == extended
    assert semantic_key(original) != semantic_key(extended)


def test_responses_uses_materialized_inputs_and_returns_usage(tmp_path):
    seen = []

    def response(req):
        body = json.loads(req.content)
        seen.append(body)
        assert req.headers["authorization"] == "Bearer fixture-api-key"
        assert "Explain this short source" in body["input"][0]["content"][0]["text"]
        assert body["store"] is False
        assert body["reasoning"] == {"effort": "high"}
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "model": "fixture-model",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "A complete response."}
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 25},
                    "output_tokens_details": {"reasoning_tokens": 5},
                },
            },
        )

    adapter = HTTPAPIAdapter(
        HTTPProviderConfig(
            "api-test",
            "responses",
            "https://api.example.invalid/v1",
            reasoning_efforts=("high",),
        ),
        credential=lambda: "fixture-api-key",
        transport=httpx.MockTransport(response),
    )
    result = adapter.start(
        request(tmp_path, "high"),
        Observer(),
        StopToken(tmp_path / "stop.json", target_attempt=1),
    )
    assert result.terminal_kind == ProviderTerminalKind.COMPLETED
    assert result.usage.input_tokens == 100
    assert result.usage.cached_input_tokens == 25
    assert result.diagnostics["usage_detail"]["reasoning_tokens"] == 5
    assert "fixture-api-key" not in repr(result)


def test_api_does_not_follow_redirects_or_include_error_body(tmp_path):
    calls = []

    def response(req):
        calls.append(req)
        return httpx.Response(
            302,
            headers={"location": "https://other.invalid"},
            text="fixture-private-response",
        )

    adapter = HTTPAPIAdapter(
        HTTPProviderConfig("api-test", "responses", "https://api.example.invalid/v1"),
        credential=lambda: "fixture-key",
        transport=httpx.MockTransport(response),
    )
    with pytest.raises(Exception) as caught:
        adapter.start(
            request(tmp_path),
            Observer(),
            StopToken(tmp_path / "stop.json", target_attempt=1),
        )
    assert len(calls) == 1
    assert "fixture-private-response" not in str(caught.value)


def test_cost_normalizes_cache_and_never_double_counts_reasoning():
    rates = PriceRates(Decimal("2"), Decimal("10"), Decimal("1"), Decimal("3"))
    usage = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cached_input_tokens": 100,
        "cache_write_tokens": 50,
        "input_includes_cache": True,
        "reasoning_tokens": 100,
        "reasoning_in_output": True,
    }
    assert rates.estimate(usage)["amount"] == "0.00395"
    assert rates.estimate({**usage, "cached_input_tokens": None})["amount"] is None
    assert rates.estimate({**usage, "input_includes_cache": None})["amount"] is None


def test_config_rejects_embedded_credentials_and_remote_plain_http():
    for url in (
        "https://user:password@example.invalid/v1",
        "http://example.invalid/v1",
        "https://example.invalid/v1?key=fixture",
    ):
        with pytest.raises(ValueError):
            HTTPProviderConfig("api-test", "responses", url)


def test_stateless_api_continues_through_a_bounded_host_broker(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_HOME", str(tmp_path / "ac-home"))
    from ac_llm import (
        LLMClient,
        LLMExecutionOptions,
        LLMExecutionProfile,
        ProviderGateOptions,
    )
    from ac_llm.host import (
        HostRequest,
        HostResponse,
        HostResponseStatus,
        HostTurn,
        encode_host_turn,
    )
    from ac_llm.providers.registry import ProviderRegistry

    class Broker:
        execution_identity = {"name": "fixture-read-only"}

        def execute(self, request, *, workspace):
            return HostResponse(
                HostResponseStatus.COMPLETED, result="Verified fixture evidence."
            )

    received = []

    def respond(req):
        text = json.loads(req.content)["input"][0]["content"][0]["text"]
        received.append(text)
        result = (
            encode_host_turn(
                HostTurn(
                    "request_host",
                    None,
                    HostRequest(
                        "read-evidence", "Read approved evidence", "Ground the answer"
                    ),
                )
            )
            if len(received) == 1
            else encode_host_turn(HostTurn("complete", "A grounded answer.", None))
        )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(result)}
                        ],
                    }
                ],
            },
        )

    registry = ProviderRegistry()
    registry.register(
        "api-fixture",
        lambda: HTTPAPIAdapter(
            HTTPProviderConfig(
                "api-fixture", "responses", "https://fixture.invalid/v1"
            ),
            credential=lambda: "fixture-key",
            transport=httpx.MockTransport(respond),
        ),
    )
    options = LLMExecutionOptions(
        profile=LLMExecutionProfile.LOCAL_APP,
        host_broker=Broker(),
        gate=ProviderGateOptions(minimum_available_memory_fraction=None),
    )
    outcome = LLMClient(registry=registry).generate(
        LLMRequest(
            "host-proof",
            "Answer using approved evidence.",
            TextOutput(),
            ModelSelection("api-fixture", "fixture-model"),
        ),
        run_root=tmp_path,
        options=options,
    )
    assert outcome.snapshot.status.value == "succeeded", outcome
    assert len(received) == 2
    assert "Verified fixture evidence." in received[1]


def test_api_does_not_persist_an_echoed_credential(tmp_path):
    secret = "fixture-credential-that-must-not-be-persisted"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Echo: " + secret}],
                    }
                ],
            },
        )
    )
    adapter = HTTPAPIAdapter(
        HTTPProviderConfig("api-test", "responses", "https://api.example.invalid/v1"),
        credential=lambda: secret,
        transport=transport,
    )
    result = adapter.start(
        request(tmp_path),
        Observer(),
        StopToken(tmp_path / "stop.json", target_attempt=1),
    )
    assert secret not in repr(result)
    assert "[redacted]" in repr(result)


def test_failed_http_call_closes_progress_before_next_success(tmp_path, monkeypatch):
    from ac_llm import LLMClient, LLMExecutionOptions, LLMExecutionProfile, ProviderGateOptions
    from ac_llm.host import HostTurn, encode_host_turn
    from ac_llm.providers.registry import ProviderRegistry
    from ac_llm.progress import DurableProviderObserver
    monkeypatch.setenv('AC_HOME', str(tmp_path/'home'))
    events = []
    original = DurableProviderObserver.progress
    def progress(self, kind, data):
        events.append((kind, {**data, **self.metadata}))
        original(self, kind, data)
    monkeypatch.setattr(DurableProviderObserver, 'progress', progress)
    count = 0
    def respond(req):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(400, json={'error':'fixture failure'})
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(encode_host_turn(HostTurn('complete','Done.',None)))}]}]})
    registry = ProviderRegistry()
    registry.register('api-fixture',lambda: HTTPAPIAdapter(
        HTTPProviderConfig('api-fixture','responses','https://fixture.invalid/v1'),
        credential=lambda:'fixture-key',transport=httpx.MockTransport(respond)))
    options = LLMExecutionOptions(profile=LLMExecutionProfile.LOCAL_APP,
        gate=ProviderGateOptions(minimum_available_memory_fraction=None))
    client = LLMClient(registry=registry)
    for task in ('failed','succeeded'):
        result = client.generate(LLMRequest(task,'Answer.',TextOutput(),ModelSelection('api-fixture','fixture')),
            run_root=tmp_path,options=options)
    assert result.snapshot.status.value == 'succeeded'
    active = set()
    failed = []
    for kind,data in events:
        if kind == 'llm_call_started': active.add(data['call_id'])
        if kind in {'llm_provider_failed','llm_usage'}: active.discard(data['call_id'])
        if kind == 'llm_provider_failed': failed.append(data)
    assert not active
    assert len(failed) == 1 and failed[0]['task_id'] == 'failed'
    assert failed[0]['category'] == 'invalid_request'


@pytest.mark.parametrize("provider,key", [
    ("codex", "OPENAI_BASE_URL"), ("codex", "CODEX_BASE_URL"),
    ("codex", "OPENAI_API_KEY"), ("codex", "CODEX_API_KEY"),
    ("claude", "ANTHROPIC_BASE_URL"), ("claude", "ANTHROPIC_API_KEY"),
    ("claude", "ANTHROPIC_AUTH_TOKEN"), ("claude", "CLAUDE_CODE_USE_BEDROCK"),
    ("claude", "CLAUDE_CODE_USE_VERTEX"), ("claude", "CLAUDE_CODE_USE_FOUNDRY"),
])
@pytest.mark.parametrize("operation", ["start", "resume"])
def test_official_cli_rejects_environment_override(tmp_path, provider, key, operation):
    from types import SimpleNamespace
    from ac_llm.providers.codex import CodexAdapter
    from ac_llm.providers.claude import ClaudeAdapter
    from ac_llm.errors import ProviderFailure
    adapter = CodexAdapter() if provider == 'codex' else ClaudeAdapter()
    req = replace(request(tmp_path), capabilities={'execution_profile': 'local_app'},
                  environment={key: 'fixture-sensitive-value'})
    with pytest.raises(ProviderFailure) as caught:
        if operation == 'start': adapter.start(req, Observer(), StopToken(tmp_path / "stop", target_attempt=1))
        else: adapter.resume(SimpleNamespace(value='fixture-session'), req, Observer(), StopToken(tmp_path / "stop", target_attempt=1))
    assert 'fixture-sensitive-value' not in str(caught.value)


def test_local_app_checks_inherited_environment(monkeypatch):
    from ac_llm.providers._cli import validate_local_app_environment
    from ac_llm.errors import ProviderFailure
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://fixture.invalid')
    with pytest.raises(ProviderFailure): validate_local_app_environment('claude', None)
    validate_local_app_environment('claude', {})
