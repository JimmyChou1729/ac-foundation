import json
from types import SimpleNamespace

import pytest

from ac_llm import claude_connection_environment, ProviderFailure
from ac_llm.providers.claude import ClaudeAdapter


def test_connection_allowlist_and_precedence(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'env': {
        'ANTHROPIC_BASE_URL': 'https://fixture.invalid',
        'ANTHROPIC_AUTH_TOKEN': 'fixture-token',
        'ANTHROPIC_DEFAULT_SONNET_MODEL': 'fixture-model',
        'BASH_ENV': 'do-not-load', 'UNRELATED': 'do-not-load',
    }, 'hooks': {'PreToolUse': 'do-not-execute'}}))
    env = claude_connection_environment({'PATH': '/fixture', 'ANTHROPIC_AUTH_TOKEN': 'override'}, config_path=path)
    assert env['ANTHROPIC_AUTH_TOKEN'] == 'override'
    assert env['ANTHROPIC_BASE_URL'] == 'https://fixture.invalid'
    assert env['ANTHROPIC_DEFAULT_SONNET_MODEL'] == 'fixture-model'
    assert 'BASH_ENV' not in env and 'UNRELATED' not in env


@pytest.mark.parametrize('value', [[], {'env': []}, {'apiKeyHelper': 'echo secret'},
    {'env': {'ANTHROPIC_AUTH_TOKEN': 12}}, {'env': {'ANTHROPIC_BASE_URL': 'https://user:secret@example.com'}},
    {'env': {'CLAUDE_CODE_USE_VERTEX': '1'}}])
def test_invalid_settings_fail_without_values(tmp_path, value):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(value))
    with pytest.raises(ProviderFailure) as caught:
        claude_connection_environment({}, config_path=path)
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('operation', ['start', 'resume'])
def test_isolated_claude_forwards_connection_only_in_environment(tmp_path, monkeypatch, operation):
    (tmp_path/'settings.json').write_text(json.dumps({'env': {'ANTHROPIC_AUTH_TOKEN': 'fixture-private-token', 'ANTHROPIC_BASE_URL': 'https://fixture.invalid'}}))
    req = SimpleNamespace(prompt='fixture prompt', model='sonnet', capabilities={'execution_profile': 'local_app'},
        environment={'CLAUDE_CONFIG_DIR': str(tmp_path)}, workspace=tmp_path,
        idle_timeout_seconds=10, reasoning_effort='medium', output_schema=None)
    monkeypatch.setattr('ac_llm.providers.materialized.materialized_prompt', lambda _: ('fixture material', []))
    adapter = ClaudeAdapter()
    calls = []
    adapter._run = lambda *args, **kwargs: calls.append(args)
    if operation == 'start': adapter.start(req, None, None)
    else: adapter.resume(SimpleNamespace(value='fixture-session'), req, None, None)
    argv, prompt, _, _, env, *_ = calls[0]
    assert env['ANTHROPIC_AUTH_TOKEN'] == 'fixture-private-token'
    assert 'fixture-private-token' not in repr(argv) + prompt
    assert argv[argv.index('--setting-sources') + 1] == ''
    assert argv[argv.index('--tools') + 1] == ''
    assert '--strict-mcp-config' in argv
