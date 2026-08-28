"""M7/P8 TDD 测试:QC 硬门禁、媒体探测、占位/预览隔离与硬链接发布。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from doc2video.config import load_config
from doc2video.contracts import (
    Block,
    ChapterPlanItem,
    ChapterSummary,
    Coverage,
    Cue,
    GroundedSummary,
    ParsedDocument,
    ParsedMeta,
    Script,
    Section,
    StageStatus,
    Subtitles,
)
from doc2video.pipeline.p7_render import stage_p7
from doc2video.pipeline.p8_qc import stage_p8
from doc2video.pipeline.runner import new_state, run_stages
from doc2video.pipeline.stages import StageResult
from doc2video.state import StateStore, sha256_file

from .test_m6_p7 import _stage_inputs


def _write_grounding(job: Path) -> None:
    parsed = ParsedDocument(
        meta=ParsedMeta(source="demo.md", type="md", pages=1, chars=100,
                        parser_version="0.1.0"),
        title="测试文档",
        sections=[Section(
            id="s1", level=1, heading="一",
            blocks=[Block(block_id="b1", type="paragraph", text="核心内容", page=1, reading_order=1)],
        )],
    )
    summary = GroundedSummary(
        doc_summary="测试摘要", key_points=[],
        chapter_plan=[ChapterPlanItem(chapter="一", section_ids=["s1"], planned_scenes=1)],
        chapter_summaries=[ChapterSummary(section_ids=["s1"], summary="本章摘要", source_block_ids=["b1"])],
        facts=[], coverage=Coverage(blocks_seen=1, blocks_total=1),
    )
    (job / "parsed.json").write_text(parsed.model_dump_json(), encoding="utf-8")
    (job / "grounded_summary.json").write_text(summary.model_dump_json(), encoding="utf-8")


def _rendered_job(tmp_path: Path) -> Path:
    job = _stage_inputs(tmp_path)
    _write_grounding(job)
    result = stage_p7(job, load_config(), SimpleNamespace())
    assert not result.needs_review
    scene_plan = json.loads((job / "scene_plan.json").read_text(encoding="utf-8"))
    scene = scene_plan["scenes"][0]
    script = Script(scenes=[{
        "id": scene["id"], "chapter": scene["chapter"], "narration": scene["narration"],
        "est_duration_s": scene["est_duration_s"], "source_block_ids": scene["source_block_ids"],
        "source_pages": scene.get("source_pages", []), "claims": [],
    }])
    (job / "script.json").write_text(script.model_dump_json(), encoding="utf-8")
    subtitles = Subtitles(cues=[Cue(
        id="sc01-cu001", scene_id="sc01", text=scene["narration"],
        start_s=0.2, end_s=2.2, source="aligned",
    )])
    (job / "subtitles.json").write_text(subtitles.model_dump_json(), encoding="utf-8")
    return job


def test_p8_pass_writes_qc_and_promotes_by_hardlink(tmp_path: Path):
    job = _rendered_job(tmp_path)
    result = stage_p8(job, load_config(), SimpleNamespace(preview=False))
    assert not result.needs_review and result.error is None
    qc = json.loads((job / "qc_report.json").read_text(encoding="utf-8"))
    assert qc["status"] in {"succeeded", "succeeded_with_warnings"}
    assert qc["publish_allowed"] is True, "S2.1:总发布决定必须入报告"
    assert {x["result"] for x in qc["checks"]} <= {"pass", "warn"}
    release = json.loads((job / "release_manifest.json").read_text(encoding="utf-8"))
    final = job / release["final_path"]
    source = job / "render" / "final.mp4"
    assert final.exists() and source.exists()
    assert os.path.samefile(final, source)
    assert release["staging_sha256"] == sha256_file(source)
    assert any("decode" in x["name"] for x in qc["checks"])
    assert any("字幕" in x["name"] for x in qc["checks"])


def test_p8_placeholder_needs_review_and_never_releases(tmp_path: Path):
    job = _rendered_job(tmp_path)
    assets = json.loads((job / "assets_manifest.json").read_text(encoding="utf-8"))
    assets["scenes"][0]["image"]["placeholder"] = True
    (job / "assets_manifest.json").write_text(json.dumps(assets), encoding="utf-8")
    result = stage_p8(job, load_config(), SimpleNamespace(preview=False))
    assert result.needs_review and result.error is None
    qc = json.loads((job / "qc_report.json").read_text(encoding="utf-8"))
    assert qc["status"] == "needs_review"
    assert not (job / "final" / "output.mp4").exists()
    assert not (job / "release_manifest.json").exists()


def test_p8_preview_never_releases_even_when_checks_pass(tmp_path: Path):
    job = _rendered_job(tmp_path)
    result = stage_p8(job, load_config(), SimpleNamespace(preview=True))
    assert result.needs_review and result.error is None
    qc = json.loads((job / "qc_report.json").read_text(encoding="utf-8"))
    assert qc["status"] == "needs_review"
    assert not (job / "final" / "output.mp4").exists()


def test_p8_decode_failure_is_failed_and_keeps_qc_report(tmp_path: Path):
    job = _rendered_job(tmp_path)
    candidate = job / "render" / "final.mp4"
    candidate.write_bytes(b"not an mp4")
    result = stage_p8(job, load_config(), SimpleNamespace(preview=False))
    assert result.error
    assert not result.needs_review
    qc = json.loads((job / "qc_report.json").read_text(encoding="utf-8"))
    assert qc["status"] == "failed"
    assert any(x["result"] == "fail" for x in qc["checks"])
    assert not (job / "final" / "output.mp4").exists()


def test_p8_duration_check_uses_timeline_not_audio_sum(tmp_path: Path):
    job = _rendered_job(tmp_path)
    timeline = json.loads((job / "render_timeline.json").read_text(encoding="utf-8"))
    timeline["total_s"] = 10.0
    (job / "render_timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    result = stage_p8(job, load_config(), SimpleNamespace(preview=False))
    assert result.error
    qc = json.loads((job / "qc_report.json").read_text(encoding="utf-8"))
    duration = next(x for x in qc["checks"] if "时长" in x["name"])
    assert duration["result"] == "fail"
    assert not (job / "final" / "output.mp4").exists()


def _add_fact_and_claim(job: Path, value: float) -> None:
    """给 _rendered_job 补一个数字事实与讲稿 claim(数字复验测试用)。"""
    summary = json.loads((job / "grounded_summary.json").read_text(encoding="utf-8"))
    summary["facts"] = [{
        "fact_id": "f01", "kind": "number", "text": "半衰期为 5.5 小时",
        "normalized": {"value": value, "unit": "小时"},
        "source_block_ids": ["b1"], "source_pages": [1],
    }]
    (job / "grounded_summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    script = json.loads((job / "script.json").read_text(encoding="utf-8"))
    narration = script["scenes"][0]["narration"]
    script["scenes"][0]["claims"] = [{"fact_id": "f01", "quote": narration[:12]}]
    (job / "script.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")


def test_p8_number_recheck_passes_when_anchored(tmp_path: Path):
    """S2.2(H-06):归一化值与原文一致 → 复验通过。"""
    job = _rendered_job(tmp_path)
    _add_fact_and_claim(job, value=5.5)
    result = stage_p8(job, load_config(), SimpleNamespace(preview=False))
    assert result.error is None
    qc = json.loads((job / "qc_report.json").read_text(encoding="utf-8"))
    numbers = next(x for x in qc["checks"] if x["name"] == "数字/单位复验")
    assert numbers["result"] == "pass"


def test_p8_number_recheck_fails_on_inconsistency(tmp_path: Path):
    """S2.2(H-06):归一化值与原文数字不符 → 硬失败,不发布。"""
    job = _rendered_job(tmp_path)
    _add_fact_and_claim(job, value=6.5)
    result = stage_p8(job, load_config(), SimpleNamespace(preview=False))
    assert result.error
    qc = json.loads((job / "qc_report.json").read_text(encoding="utf-8"))
    assert qc["status"] == "failed"
    assert qc["publish_allowed"] is False
    numbers = next(x for x in qc["checks"] if x["name"] == "数字/单位复验")
    assert numbers["result"] == "fail"
    assert not (job / "final" / "output.mp4").exists()


def test_p8_uncovered_blocks_drive_needs_review(tmp_path: Path):
    """S2.2(H-06):coverage.uncovered 超阈值 → needs_review 且不发布。"""
    job = _rendered_job(tmp_path)
    summary = json.loads((job / "grounded_summary.json").read_text(encoding="utf-8"))
    summary["coverage"] = {"blocks_seen": 0, "blocks_total": 1, "uncovered_block_ids": ["b1"]}
    (job / "grounded_summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    result = stage_p8(job, load_config(), SimpleNamespace(preview=False))
    assert result.needs_review and result.error is None
    qc = json.loads((job / "qc_report.json").read_text(encoding="utf-8"))
    assert qc["status"] == "needs_review"
    assert qc["publish_allowed"] is False
    coverage = next(x for x in qc["checks"] if x["name"] == "证据覆盖")
    assert coverage["result"] == "warn"
    assert not (job / "final" / "output.mp4").exists()


def test_qc_thresholds_are_parametrized(tmp_path: Path):
    """S2.1(AC-7):阈值从 config/qc 消费——收紧时长容差即可构造违规触发。"""
    job = _rendered_job(tmp_path)
    cfg = load_config()
    cfg["qc"]["duration_tolerance"] = 0.0  # 任何误差都不允许 → 必然触发
    result = stage_p8(job, cfg, SimpleNamespace(preview=False))
    assert result.error
    qc = json.loads((job / "qc_report.json").read_text(encoding="utf-8"))
    duration = next(x for x in qc["checks"] if x["name"] == "时长")
    assert duration["result"] == "fail"


def test_runner_commits_error_stage_artifacts_before_failed(tmp_path: Path, monkeypatch):
    from doc2video.pipeline import runner

    job = tmp_path / "job"
    job.mkdir()
    state = new_state("20260827_ab12cd")
    for stage in runner.STAGES[:8]:
        state.stages[stage].status = StageStatus.succeeded
    StateStore(job).save(state)

    def fake_handler(job_dir, cfg, opts, stage=None):
        (job_dir / "qc_report.json").write_text('{"status":"failed"}\n', encoding="utf-8")
        (job_dir / "egress_report.json").write_text('{"calls":[]}\n', encoding="utf-8")
        return StageResult(
            artifacts=[("qc_report.json", "application/json"), ("egress_report.json", "application/json")],
            error="硬门禁失败",
        )

    monkeypatch.setattr(runner, "stage_handler", lambda stage: fake_handler)
    final = run_stages(job, load_config(), SimpleNamespace())
    assert final.stages["P8"].status == StageStatus.failed
    ref = final.stages["P8"].artifact_manifest_ref
    assert ref == "artifact_manifest.P8.json"
    assert (job / ref).exists()
