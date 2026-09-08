from __future__ import annotations

import json

from ac_llm.model_catalog import codex_model_catalog


class _Input:
    def __init__(self):
        self.values: list[str] = []

    def write(self, value):
        self.values.append(value)

    def flush(self):
        pass


class _Process:
    def __init__(self, lines):
        self.stdin = _Input()
        self.stdout = iter(lines)
        self.terminated = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        return 0

    def kill(self):
        self.terminated = True


def _response(models):
    return json.dumps({"id": 2, "result": {"data": models}}) + "\n"


def _model(model="gpt-fixture", *, efforts=("low", "high"), hidden=False):
    return {
        "model": model,
        "displayName": "Fixture model",
        "description": "Fixture description",
        "hidden": hidden,
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort, "description": effort} for effort in efforts
        ],
        "defaultReasoningEffort": efforts[0],
        "isDefault": True,
    }


def test_codex_catalog_reads_visible_models_and_efforts(monkeypatch):
    process = _Process(
        [json.dumps({"id": 1, "result": {}}) + "\n", _response([_model()])]
    )
    monkeypatch.setattr(
        "ac_llm.model_catalog.subprocess.Popen", lambda *a, **k: process
    )

    catalog = codex_model_catalog("fixture-codex")

    assert catalog.status == "available"
    assert catalog.source == "codex_cli"
    assert catalog.models[0].model == "gpt-fixture"
    assert catalog.models[0].reasoning_efforts == ("low", "high")
    assert process.terminated
    requests = [json.loads(value) for value in process.stdin.values]
    assert requests[-1] == {
        "method": "model/list",
        "id": 2,
        "params": {"limit": 100, "includeHidden": False},
    }


def test_codex_catalog_fails_closed_on_invalid_or_empty_data(monkeypatch):
    process = _Process([_response([_model("bad model")])])
    monkeypatch.setattr(
        "ac_llm.model_catalog.subprocess.Popen", lambda *a, **k: process
    )

    catalog = codex_model_catalog()

    assert catalog.status == "unavailable"
    assert catalog.models == ()
    assert catalog.message == "无法从本机 Codex CLI 读取可用模型。"
