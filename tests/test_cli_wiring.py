"""S2.4 CLI 接线测试(AC-10):每个 flag 传参后断言下游行为改变,杜绝"选项被收集但无消费方"。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from doc2video.cli import main
from doc2video.config import load_config
from doc2video.pipeline import p5_assets
from doc2video.pipeline.p7_render import stage_p7
from doc2video.pipeline.p9_jianying import stage_p9

from .fake_llm import FakeLLM
from .test_cli import _FAKE_RESPONSES
from .test_m6_p7 import _stage_inputs, _wav
from .test_m7_p8 import _rendered_job


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: FakeLLM(_FAKE_RESPONSES))


def _make_doc(tmp_path: Path) -> Path:
    doc = tmp_path / "demo.md"
    doc.write_text("# 标题\n\n正文内容。", encoding="utf-8")
    return doc


def _latest_job(ws: Path) -> Path:
    jobs = sorted(p for p in ws.rglob("*") if p.is_dir() and (p / "run_options.json").exists())
    assert jobs, "run 未生成作业目录"
    return jobs[-1]


def test_flag_voice_reaches_tts_config(tmp_path: Path, monkeypatch):
    seen: list[str] = []

    def capture(text, out_path, cfg, privacy_mode, job_dir):
        seen.append(cfg["tts"]["voice"])
        p5_assets._write_silence(out_path, 1.5)
        return {"provider": "capture", "voice": cfg["tts"]["voice"], "placeholder": True}

    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setattr(p5_assets, "make_audio", capture)
    assert main(["run", str(_make_doc(tmp_path)), "--privacy", "offline",
                 "--voice", "zh-CN-YunxiNeural"]) == 3
    assert seen == ["zh-CN-YunxiNeural"]


def test_flag_bgm_snapshotted_into_job(tmp_path: Path, monkeypatch):
    """L-04:BGM 快照入 Job,run_options 存 Job 内相对路径(可复现/可续跑)。"""
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    bgm = tmp_path / "music.wav"
    _wav(bgm, 1.0)
    assert main(["run", str(_make_doc(tmp_path)), "--privacy", "offline", "--bgm", str(bgm)]) == 3
    job = _latest_job(tmp_path / "ws")
    assert (job / "bgm" / "music.wav").exists()
    opts = json.loads((job / "run_options.json").read_text(encoding="utf-8"))
    assert opts["bgm"] == "bgm/music.wav"


def test_flag_bgm_missing_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    assert main(["run", str(_make_doc(tmp_path)), "--bgm", str(tmp_path / "nope.wav")]) == 2


def test_flag_preview_writes_isolated_dir(tmp_path: Path, monkeypatch):
    """S2.6:preview 子命令 → 独立目录 + preview 强制标记。"""
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    assert main(["preview", str(_make_doc(tmp_path)), "--privacy", "offline"]) == 3
    preview_root = tmp_path / "ws" / "preview"
    jobs = [p for p in preview_root.iterdir() if p.is_dir() and (p / "run_options.json").exists()]
    assert len(jobs) == 1, "preview 作业必须写入独立目录"
    opts = json.loads((jobs[0] / "run_options.json").read_text(encoding="utf-8"))
    assert opts["preview"] is True
    assert not (jobs[0] / "final" / "output.mp4").exists()


def test_flag_style_aspect_consumed_by_scene_plan(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    assert main(["run", str(_make_doc(tmp_path)), "--privacy", "offline",
                 "--style", "tech-dark", "--aspect", "9:16"]) == 3
    job = _latest_job(tmp_path / "ws")
    plan = json.loads((job / "scene_plan.json").read_text(encoding="utf-8"))
    assert plan["style"]["name"] == "tech-dark"
    assert plan["scenes"] and all(s["aspect"] == "9:16" for s in plan["scenes"])


def test_flag_jobs_gt1_warns_and_degrades(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    code = main(["run", str(_make_doc(tmp_path)), "--privacy", "offline", "--jobs", "2"])
    assert code == 3
    assert "降级" in capsys.readouterr().err


# ── P7/P9 消费方接线 ──────────────────────────────────────────────


def test_flag_bgm_consumed_by_p7(tmp_path: Path):
    job = _stage_inputs(tmp_path)
    bgm = tmp_path / "bgm.wav"
    _wav(bgm, 1.0, freq=330)
    result = stage_p7(job, load_config(), SimpleNamespace(bgm=str(bgm)))
    assert not result.needs_review
    assert (job / "render" / "mixed.mp4").exists()
    manifest = json.loads((job / "render_manifest.json").read_text(encoding="utf-8"))
    assert "-stream_loop" in manifest["bgm_mix_argv"], "BGM 混音命令必须入审计清单"


def test_flag_bgm_job_relative_resolved_in_job(tmp_path: Path):
    """L-04:Job 内相对路径 BGM(快照)在 P7 按 job_dir 解析。"""
    job = _stage_inputs(tmp_path)
    (job / "bgm").mkdir()
    _wav(job / "bgm" / "music.wav", 1.0, freq=330)
    result = stage_p7(job, load_config(), SimpleNamespace(bgm="bgm/music.wav"))
    assert not result.needs_review
    assert (job / "render" / "mixed.mp4").exists()


def test_flag_no_burn_subs_skips_ass_burn(tmp_path: Path):
    job = _stage_inputs(tmp_path)
    result = stage_p7(job, load_config(), SimpleNamespace(no_burn_subs=True))
    assert not result.needs_review
    assert (job / "render" / "final.mp4").exists()
    assert (job / "render" / "subtitles.ass").exists(), "软字幕 ASS 仍应保留"
    manifest = json.loads((job / "render_manifest.json").read_text(encoding="utf-8"))
    assert not any("ass=" in a for a in manifest["command_argv"])


def test_font_recorded_in_render_manifest(tmp_path: Path):
    """L-04:字体使用记录入 render_manifest(可审计/可复现)。"""
    job = _stage_inputs(tmp_path)
    stage_p7(job, load_config(), SimpleNamespace())
    manifest = json.loads((job / "render_manifest.json").read_text(encoding="utf-8"))
    assert manifest["fonts"] == ["Microsoft YaHei"]


def test_flag_export_draft_consumed_by_p9(tmp_path: Path):
    job = _rendered_job(tmp_path)
    result = stage_p9(job, load_config(), SimpleNamespace(export_draft=True))
    assert result.error is None
    report = json.loads((job / "draft_export_report.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert (job / report["draft_path"]).is_dir()


# ── S2.3 字幕真值评测基建 ─────────────────────────────────────────


def test_subtitle_eval_no_truth_reports_no_metrics(tmp_path: Path):
    from scripts.subtitle_eval import evaluate

    job = _rendered_job(tmp_path)
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps({"target_ms": 300, "segments": []}), encoding="utf-8")
    report = evaluate(job, truth)
    assert report["status"] == "no_truth"
    assert "不造假指标" in report["note"]


def test_subtitle_eval_measures_errors_against_truth(tmp_path: Path):
    from scripts.subtitle_eval import evaluate

    job = _rendered_job(tmp_path)
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps({
        "target_ms": 300,
        "segments": [{"text": "核心内容", "start_s": 0.25, "end_s": 2.1, "scene_id": "sc01"}],
    }), encoding="utf-8")
    report = evaluate(job, truth)
    assert report["status"] == "measured"
    m = report["metrics"]
    assert m["coverage"] == 1.0
    assert m["start_err_p95_s"] == pytest.approx(0.05, abs=1e-6)
    assert m["end_err_p95_s"] == pytest.approx(0.1, abs=1e-6)
    assert "达标" in report["verdict"]
