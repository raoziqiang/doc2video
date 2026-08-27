"""M3/P4 TDD 测试:分镜规划、style bible、视觉来源判定与契约门禁。"""

from __future__ import annotations

from pathlib import Path

import pytest

from doc2video.config import load_config
from doc2video.contracts import GroundedSummary, ParsedDocument, ScenePlan, Script
from doc2video.pipeline.p4_scene_plan import choose_visual_source, load_style_template, stage_p4

from . import fixtures as fx
from .fake_llm import FakeLLM


@pytest.fixture()
def p4_job(tmp_path: Path) -> Path:
    cfg = load_config()
    src = fx.make_docx(tmp_path)
    from doc2video.pipeline.p1_parser import parse_document

    parsed = parse_document(src, tmp_path, "docx", cfg)
    job = tmp_path / "20260827_p4test"
    job.mkdir()
    (job / "parsed.json").write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
    (job / "script.json").write_text(_script_json(parsed).model_dump_json(indent=2), encoding="utf-8")
    (job / "grounded_summary.json").write_text(_summary_json(parsed).model_dump_json(indent=2), encoding="utf-8")
    # P1 实际提取的内嵌图片路径已在 parsed 中;构造 P4 可识别的 extracted_assets 目录
    (job / "extracted_assets").mkdir()
    return job


def _script_json(parsed: ParsedDocument) -> Script:
    from doc2video.contracts import ScriptScene

    return Script(scenes=[ScriptScene(
        id="sc01", chapter="咖啡因与睡眠",
        narration=("大家好,今天我们用五分钟解读咖啡因与睡眠的关系,先看它如何影响大脑,"
                   "再看半衰期和饮用时间,最后给出实用建议帮助大家改善睡眠。"),
        est_duration_s=13.3, source_block_ids=["b1"], source_pages=[1],
    )])


def _summary_json(parsed: ParsedDocument) -> GroundedSummary:
    from doc2video.contracts import ChapterPlanItem, ChapterSummary, Coverage

    bids = [b.block_id for s in parsed.sections for b in s.blocks if b.text]
    return GroundedSummary(
        doc_summary="本文介绍咖啡因影响睡眠的机制和关键时间数据,并给出实践建议。",
        key_points=[],
        chapter_plan=[ChapterPlanItem(chapter="咖啡因与睡眠", section_ids=["s1", "s2"], planned_scenes=1)],
        chapter_summaries=[ChapterSummary(section_ids=["s1", "s2"], summary="机制与数据。", source_block_ids=bids[:1])],
        facts=[], coverage=Coverage(blocks_seen=len(bids), blocks_total=len(bids)),
    )


def test_style_bible_loads_known_template():
    style = load_style_template(load_config(), "flat-illustration")
    assert style.name == "flat-illustration"
    assert "深蓝" in style.prefix
    assert "text" in style.negative


def test_unknown_style_fails_closed():
    with pytest.raises(ValueError, match="未知 style"):
        load_style_template(load_config(), "does-not-exist")


def test_visual_source_prefers_extracted_image():
    source, ref = choose_visual_source(
        block_types={"b1": "image", "b2": "paragraph"},
        source_block_ids=["b1", "b2"],
        extracted_refs={"b1": "extracted_assets/docx_rId9.png"},
    )
    assert source == "extracted_image"
    assert ref == "extracted_assets/docx_rId9.png"


def test_visual_source_uses_rendered_table():
    source, ref = choose_visual_source(
        block_types={"b1": "table"}, source_block_ids=["b1"], extracted_refs={}
    )
    assert source == "rendered_table"
    assert ref is not None and ref.startswith("derived/table-")


def test_visual_source_generated_for_concept_only():
    source, ref = choose_visual_source(
        block_types={"b1": "paragraph"}, source_block_ids=["b1"], extracted_refs={}
    )
    assert source == "generated" and ref is None


def test_stage_p4_writes_schema_valid_scene_plan(p4_job: Path, monkeypatch):
    response = {"scenes": [{
        "id": "sc01",
        "visual_desc": "咖啡杯与睡眠压力波形的扁平插画,暖色光线,无文字。",
        "image_prompt": "扁平插画,咖啡杯与大脑中的睡眠压力波形,深蓝橙色,无文字",
    }]}
    fake = FakeLLM(responses={"【分镜输入】": response})
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: fake)
    result = stage_p4(p4_job, load_config(), type("Opts", (), {"style": "flat-illustration"})())
    assert not result.needs_review
    plan = ScenePlan.model_validate_json((p4_job / "scene_plan.json").read_text(encoding="utf-8"))
    assert len(plan.scenes) == 1
    assert plan.scenes[0].image_prompt
    assert plan.scenes[0].source_block_ids == ["b1"]


def test_stage_p4_rejects_missing_scene(p4_job: Path, monkeypatch):
    response = {"scenes": [{
        "id": "sc99",
        "visual_desc": "额外场景,不属于讲稿输入。",
        "image_prompt": "一个无关的城市夜景",
    }]}
    fake = FakeLLM(responses={"【分镜输入】": response})
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: fake)
    result = stage_p4(p4_job, load_config(), type("Opts", (), {"style": "flat-illustration"})())
    assert result.needs_review
    assert result.warnings
