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
