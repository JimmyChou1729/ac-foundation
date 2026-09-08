from types import SimpleNamespace

import pytest

from ac_llm.providers.claude import ClaudeAdapter


@pytest.mark.parametrize("profile", ["standard", "bounded", "local_app"])
@pytest.mark.parametrize("resume", [False, True])
def test_claude_effort_reaches_cli_in_every_profile(monkeypatch, profile, resume):
    monkeypatch.setattr("ac_llm.providers.materialized.materialized_prompt", lambda workspace: ("materialized", []))
    adapter = ClaudeAdapter(binary="claude-fixture")
    captured = []
    monkeypatch.setattr(adapter, "_run", lambda argv, *args, **kwargs: captured.append(argv))
    request = SimpleNamespace(
        prompt="prompt", model="fixture", reasoning_effort="high",
        capabilities={"execution_profile": profile, "reasoning_effort": "high"},
        output_schema=None, idle_timeout_seconds=None, workspace=None, environment={},
    )
    if resume:
        adapter.resume(SimpleNamespace(value="session"), request, None, None)
    else:
        adapter.start(request, None, None)
    assert captured[0].count("--effort") == 1
    assert captured[0][captured[0].index("--effort") + 1] == "high"
