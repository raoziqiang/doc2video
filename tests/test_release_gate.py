"""M9 发布级 gate 测试:fail-closed、凭据脱敏、报告结构与命令安全;S3.1 候选产物绑定与不可绕过。"""

from __future__ import annotations

import json
from pathlib import Path

from doc2video.pipeline.stages import StageResult
from scripts.release_gate import (
    GateCheck,
    _known_secrets,
    build_report,
    candidate_descriptor,
    check_edge_tts,
    check_fal,
    check_media_smoke,
    check_schemas,
    credential_state,
    live_scope_check,
    main,
    overall_status,
    redact,
    verify_candidate,
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


# ── S3.1 候选产物绑定与不可绕过(AC-11) ─────────────────────────


def _write_bound_report(root: Path, candidate: Path, ready: bool) -> Path:
    release_dir = root / "docs" / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    report = build_report([GateCheck("unit", "pass", "ok")], "0.1.0",
                          candidate_descriptor(candidate))
    report["release_ready"] = ready
    if not ready:
        report["status"] = "blocked"
    path = release_dir / "release_gate.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


def test_candidate_descriptor_binds_sha256(tmp_path: Path):
    from doc2video.state import sha256_file

    artifact = tmp_path / "output.mp4"
    artifact.write_bytes(b"video-bytes")
    desc = candidate_descriptor(artifact)
    assert desc["sha256"] == sha256_file(artifact)
    assert desc["size"] == len(b"video-bytes")
    assert candidate_descriptor(None) is None


def test_candidate_descriptor_rejects_missing_file(tmp_path: Path):
    try:
        candidate_descriptor(tmp_path / "missing.mp4")
        raise AssertionError("不存在的候选产物必须被拒绝")
    except FileNotFoundError:
        pass


def test_verify_passes_when_digest_bound_and_ready(tmp_path: Path):
    artifact = tmp_path / "output.mp4"
    artifact.write_bytes(b"release-bytes")
    _write_bound_report(tmp_path, artifact, ready=True)
    ok, detail = verify_candidate(tmp_path, artifact)
    assert ok and "gate 全绿" in detail
    assert main(["verify", str(artifact)], root=tmp_path) == 0


def test_verify_rejects_after_artifact_tampering(tmp_path: Path):
    artifact = tmp_path / "output.mp4"
    artifact.write_bytes(b"release-bytes")
    _write_bound_report(tmp_path, artifact, ready=True)
    artifact.write_bytes(b"tampered-bytes")  # 产物被篡改 → digest 不符必须拒绝
    ok, detail = verify_candidate(tmp_path, artifact)
    assert not ok and "无 gate 报告绑定" in detail
    assert main(["verify", str(artifact)], root=tmp_path) == 2


def test_verify_rejects_when_gate_not_ready(tmp_path: Path):
    artifact = tmp_path / "output.mp4"
    artifact.write_bytes(b"release-bytes")
    _write_bound_report(tmp_path, artifact, ready=False)
    ok, detail = verify_candidate(tmp_path, artifact)
    assert not ok and "不可发布" in detail


def test_verify_rejects_without_any_gate_report(tmp_path: Path):
    artifact = tmp_path / "output.mp4"
    artifact.write_bytes(b"release-bytes")
    ok, _detail = verify_candidate(tmp_path, artifact)
    assert not ok
    (tmp_path / "docs" / "release").mkdir(parents=True)
    ok, detail = verify_candidate(tmp_path, artifact)
    assert not ok and "必须先运行" in detail


def test_verify_rejects_missing_candidate(tmp_path: Path):
    (tmp_path / "docs" / "release").mkdir(parents=True)
    ok, detail = verify_candidate(tmp_path, tmp_path / "missing.mp4")
    assert not ok and "不存在" in detail


def test_gate_cli_exposes_no_bypass_flags(capsys):
    """不可手工绕过:CLI 不得提供 skip/force/yes 类旁路参数。"""
    try:
        main(["--help"])
    except SystemExit:
        pass
    help_text = capsys.readouterr().out
    for bypass in ("--skip", "--force", "--yes", "--no-gate"):
        assert bypass not in help_text, f"gate 不得提供旁路参数 {bypass}"

