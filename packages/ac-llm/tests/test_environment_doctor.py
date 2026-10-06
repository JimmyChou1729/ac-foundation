from __future__ import annotations

import json

from ac_llm import environment_diagnostics


def test_socks_diagnostics_redact_proxy_and_do_not_install(monkeypatch, tmp_path):
    monkeypatch.setattr("ac_llm.environment_doctor.importlib.util.find_spec", lambda name: None)
    report = environment_diagnostics(project_dir=tmp_path, env={
        "HTTPS_PROXY": "socks5://secret-user:secret-password@proxy.invalid:1080",
        "NO_PROXY": "private.internal",
    })
    assert report["proxy"]["status"] == "missing_optional_dependency"
    assert report["proxy"]["configured"] == [{"variable": "HTTPS_PROXY", "scheme": "socks5"}]
    assert report["write"]["status"] == "available"
    assert report["network"]["status"] == "not_checked"
    encoded = json.dumps(report)
    assert "secret" not in encoded and "proxy.invalid" not in encoded and "private.internal" not in encoded
    assert list(tmp_path.iterdir()) == []


def test_missing_write_root_and_invalid_coordinator_are_actionable(tmp_path):
    report = environment_diagnostics(project_dir=tmp_path / "missing", env={"AC_LLM_HOST_COORDINATOR": "invalid"})
    assert report["write"]["status"] == "unavailable"
    assert report["host"]["configured"] is False
    assert report["host"]["configuration_error"]


def test_proxy_without_scheme_does_not_expose_username():
    report = environment_diagnostics(env={"HTTPS_PROXY": "private-user:private-password@proxy.invalid:8080"})
    assert report["proxy"]["configured"] == [{"variable": "HTTPS_PROXY", "scheme": "unsupported_or_unspecified"}]
    assert "private" not in json.dumps(report)


def test_host_declaration_is_separate_from_actual_tool_verification():
    report = environment_diagnostics(env={"AC_LLM_HOST_COORDINATOR": json.dumps({"coordinator_id": "fixture", "fresh_context": True})})
    assert report["host"]["configured"] is True
    assert report["host"]["capabilities"]["fresh_context"] is True
    assert report["host"]["runtime_tools_verified"] is False


def test_cli_environment_doctor_returns_public_details(tmp_path, monkeypatch, capsys):
    from ac_llm.cli import main
    monkeypatch.setenv("AC_LLM_HOST_COORDINATOR", json.dumps({"coordinator_id": "fixture"}))
    assert main(["doctor", "--provider", "host", "--environment", "--project-dir", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["environment"]["write"]["status"] == "available"
    assert result["data"]["provider"] == "host"
