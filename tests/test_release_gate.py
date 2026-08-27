"""M9 发布级 gate 测试:fail-closed、凭据脱敏、报告结构与命令安全。"""

from __future__ import annotations

import json
from pathlib import Path

from doc2video.pipeline.stages import StageResult
from scripts.release_gate import (
    GateCheck,
    _known_secrets,
    build_report,
    check_edge_tts,
    check_fal,
    check_media_smoke,
    check_schemas,
    credential_state,
    live_scope_check,
    overall_status,
    redact,
)


def test_overall_status_is_fail_closed():
    assert overall_status([GateCheck("a", "pass", "ok")]) == "passed"
    assert overall_status([GateCheck("a", "warn", "warning")]) == "warnings"
    assert overall_status([GateCheck("a", "blocked", "missing")]) == "blocked"
    assert overall_status([GateCheck("a", "fail", "bad"), GateCheck("b", "blocked", "x")]) == "failed"


def test_blocked_or_failed_report_is_not_release_ready(tmp_path: Path):
    report = build_report([
        GateCheck("missing-fal", "blocked", "FAL_KEY 未配置"),
        GateCheck("tests", "pass", "ok"),
    ], project_version="0.1.0")
    assert report["status"] == "blocked"
    assert report["release_ready"] is False
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    assert "FAL_KEY" in path.read_text(encoding="utf-8")


def test_credential_state_never_returns_secret_value():
    fixture_value = "fixture" + "-secret-value-should-not-appear"
    assert credential_state(fixture_value) == "configured"
    assert credential_state("") == "missing"
    assert fixture_value not in credential_state(fixture_value)


def test_redact_removes_known_secret():
    fixture_value = "fixture" + "-secret-value-should-not-appear"
    assert redact(f"token={fixture_value}", [fixture_value]) == "token=[REDACTED]"


def test_redact_replaces_known_prefixes_safely():
    prefix = "fixture" + "-key"
    longer = prefix + "-extended"
    assert redact(f"value={longer}", [prefix, longer]) == "value=[REDACTED]"


def test_build_report_contains_stable_check_fields():
    report = build_report([GateCheck("unit", "pass", "142 passed")], project_version="0.1.0")
    assert report["schema_version"] == "1.0"
    assert report["status"] == "passed"
    assert report["release_ready"] is True
    assert report["checks"] == [{"name": "unit", "status": "pass", "detail": "142 passed"}]


def test_release_scope_requires_live_smoke():
    check = live_scope_check(False)
    assert check.status == "blocked"
    assert "--live" in check.detail
    assert live_scope_check(True).status == "pass"


def test_known_secrets_includes_key_like_environment_values(tmp_path: Path, monkeypatch):
    fixture_value = "fixture" + "-env-key-value"
    monkeypatch.setenv("EXAMPLE_API_KEY", fixture_value)
    assert fixture_value in _known_secrets(tmp_path)


def test_media_gate_converts_stage_exception_to_failed_check(tmp_path: Path, monkeypatch):
    fixture_value = "fixture" + "-media-key"
    monkeypatch.setenv("EXAMPLE_API_KEY", fixture_value)
    import scripts.release_gate as gate

    def broken_stage(*args, **kwargs):
        raise RuntimeError(f"synthetic render failure {fixture_value}")

    monkeypatch.setattr(gate, "stage_p7", broken_stage)
    result = check_media_smoke(tmp_path)
    assert result.status == "fail"
    assert fixture_value not in result.detail
    assert "[REDACTED]" in result.detail


def test_check_fal_redacts_smoke_failure_details(tmp_path: Path, monkeypatch):
    fixture_value = "fixture" + "-fal-key-to-redact"
    monkeypatch.setenv("FAL_KEY", fixture_value)

    # Mock run_command to succeed so we parse the JSON
    import scripts.release_gate as gate
    monkeypatch.setattr(gate, "run_command", lambda *args, **kwargs: GateCheck("live:FAL 直连 smoke", "pass", "ok"))

    # Create dummy fal_smoke.json under tmp_path/docs/spikes/
    spikes_dir = tmp_path / "docs" / "spikes"
    spikes_dir.mkdir(parents=True, exist_ok=True)
    report_file = spikes_dir / "fal_smoke.json"
    report_file.write_text(json.dumps({
        "smoke": {
            "ok": False,
            "detail": f"Failed with key {fixture_value} inside error message"
        }
    }), encoding="utf-8")

    result = check_fal(tmp_path)
    assert result.status == "fail"
    assert fixture_value not in result.detail
    assert "[REDACTED]" in result.detail


def test_schema_gate_converts_generation_exception_to_failed_check(tmp_path: Path, monkeypatch):
    fixture_value = "fixture" + "-schema-key"
    monkeypatch.setenv("EXAMPLE_API_KEY", fixture_value)
    import doc2video.contracts.generate_schemas as schemas

    def broken_generate(*args, **kwargs):
        raise FileNotFoundError(f"schema directory missing {fixture_value}")

    monkeypatch.setattr(schemas, "generate_all", broken_generate)
    result = check_schemas(tmp_path)
    assert result.status == "fail"
    assert fixture_value not in result.detail
    assert "[REDACTED]" in result.detail


def test_edge_tts_exception_is_redacted(tmp_path: Path, monkeypatch):
    fixture_value = "fixture" + "-tts-key"
    monkeypatch.setenv("EXAMPLE_API_KEY", fixture_value)
    import scripts.release_gate as gate

    def broken_run(coro):
        coro.close()
        raise RuntimeError(f"edge tts failure {fixture_value}")

    monkeypatch.setattr(gate.asyncio, "run", broken_run)
    result = check_edge_tts(tmp_path)
    assert result.status == "fail"
    assert fixture_value not in result.detail
    assert "[REDACTED]" in result.detail


def test_media_gate_redacts_structured_stage_error(tmp_path: Path, monkeypatch):
    fixture_value = "fixture" + "-stage-key"
    monkeypatch.setenv("EXAMPLE_API_KEY", fixture_value)
    import scripts.release_gate as gate

    def failed_stage(*args, **kwargs):
        return StageResult(error=f"P7 provider error {fixture_value}")

    monkeypatch.setattr(gate, "stage_p7", failed_stage)
    result = check_media_smoke(tmp_path)
    assert result.status == "fail"
    assert fixture_value not in result.detail
    assert "[REDACTED]" in result.detail


def test_known_secrets_survives_dotenv_read_error(tmp_path: Path, monkeypatch):
    fixture_value = "fixture" + "-dotenv-key"
    monkeypatch.setenv("EXAMPLE_API_KEY", fixture_value)
    import dotenv

    def broken_values(*args, **kwargs):
        raise OSError("cannot read dotenv")

    monkeypatch.setattr(dotenv, "dotenv_values", broken_values)
    assert fixture_value in _known_secrets(tmp_path)
