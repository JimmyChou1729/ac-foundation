import json

from ac_llm import HostCoordinator, LLMExecutionOptions, ModelSelection, resolve_execution_model


def test_coordinator_default_is_resolved_before_recipe_freezing(monkeypatch):
    monkeypatch.setenv('AC_LLM_HOST_COORDINATOR', json.dumps(HostCoordinator('work').to_document()))
    monkeypatch.setenv('CODEX_THREAD_ID', 'outer-native-host')
    model = resolve_execution_model(ModelSelection())
    assert (model.provider, model.model, model.reasoning_effort) == ('host', 'inherit', None)


def test_explicit_coordinator_and_model_preferences_take_precedence(monkeypatch):
    monkeypatch.setenv('AC_LLM_HOST_COORDINATOR', 'invalid configuration should not be consulted')
    options = LLMExecutionOptions(host_coordinator=HostCoordinator('work'))
    model = resolve_execution_model(ModelSelection('host', model='chosen-model', reasoning_effort='high'), options=options)
    assert (model.provider, model.model, model.reasoning_effort) == ('host', 'chosen-model', 'high')


def test_explicit_native_recipe_keeps_native_when_fallback_is_disabled(monkeypatch):
    options = LLMExecutionOptions(host_coordinator=HostCoordinator('work', native_fallback=False))
    model = resolve_execution_model(ModelSelection('codex', 'chosen-model'), options=options)
    assert (model.provider, model.model) == ('codex', 'chosen-model')
