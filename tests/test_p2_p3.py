"""M2 测试:P2 内容理解 / P3 讲稿生成 / 分块 / claim→fact 检查。"""

from __future__ import annotations

from pathlib import Path

import pytest

from doc2video.config import load_config
from doc2video.contracts import GroundedSummary, Script
from doc2video.pipeline.p1_parser import parse_document
from doc2video.pipeline.p2_understand import _chunk_blocks, stage_p2
from doc2video.pipeline.p3_script import check_claims, stage_p3
from doc2video.pipeline.stages import StageResult

from . import fixtures as fx
from .fake_llm import FakeLLM

NARRATION = (
    "大家好,今天我们用五分钟解读这份报告的核心结论,先看它的整体框架与关键数据。"
    "报告指出,咖啡因的半衰期是五到六小时,下午三点的咖啡到晚上九点仍有一半留在体内。"
)

CHUNK_RESP = {
    "results": [
        {"block_id": b, "summary": f"块 {b} 的摘要:内容与咖啡因有关。",
         "facts": [
             {"kind": "number", "text": "半衰期五到六小时",
              "normalized": {"value": "5.5", "unit": "小时", "polarity": "positive"}}
         ],
         "low_info": False}
        for b in ["b1", "b2", "b3", "b4", "b5", "b6"]
    ]
}

REDUCE_RESP = {
    "doc_summary": "本文介绍咖啡因如何影响睡眠,包括作用机制、半衰期数据与建议。全文要点清晰,适合口播讲解。",
    "key_points": [{"text": "半衰期五到六小时", "source_block_ids": ["b3"]}],
}


@pytest.fixture()
def parsed_job(tmp_path: Path, monkeypatch) -> Path:
    """真实解析 demo.md → 写 parsed.json 的作业目录。"""
    cfg = load_config()
    src = fx.make_md(tmp_path)
    parsed = parse_document(src, tmp_path, "md", cfg)
    job = tmp_path / "20260827_abc123"
    job.mkdir()
    (job / "parsed.json").write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
    return job


def _make_p2_responses() -> dict:
    # 按 prompt 标记返回:分块 prompt 含 【块列表】,reduce prompt 含 【分块摘要】
    return {"【块列表】": CHUNK_RESP, "【分块摘要】": REDUCE_RESP}


# ── 分块 ───────────────────────────────────────────────────


def test_chunk_blocks_respects_budget():
    blocks = [(f"b{i}", "字" * (i + 1) * 10) for i in range(1, 6)]
    tokens = [len(t) for _, t in blocks]
    chunks = _chunk_blocks(blocks, tokens, budget=60)
    assert sum(len(c) for c in chunks) == len(blocks)  # 全覆盖
    for chunk in chunks:
        assert sum(len(t) for _, t in chunk) <= 60


def test_chunk_blocks_splits_oversized_block():
    blocks = [("b1", "字" * 1000)]
    chunks = _chunk_blocks(blocks, [1000], budget=300)
    assert len(chunks) > 1
    # 切碎块 id 带 #片段号,原始内容不丢失
    joined = "".join(t for c in chunks for _, t in c)
    assert joined == "字" * 1000


# ── claim→fact ─────────────────────────────────────────────


def test_check_claims_ok():
    from doc2video.contracts import Claim, ScriptScene

    scene = ScriptScene(
        id="sc01", chapter="一", narration=NARRATION, est_duration_s=13.3,
        source_block_ids=["b3"],
        claims=[Claim(fact_id="f01", quote="半衰期是五到六小时")],
    )
    problems = check_claims([scene], {"f01": "咖啡因的半衰期五到六小时"})
    assert problems == []


def test_check_claims_decimal_token_normalization():
    from doc2video.contracts import Claim, ScriptScene

    scene = ScriptScene(
        id="sc01", chapter="一", narration=NARRATION + "报告给出的数值是5.5小时,这是关键数据。", est_duration_s=13.3,
        source_block_ids=["b1"], claims=[Claim(fact_id="f01", quote="5.5小时")],
    )
    assert check_claims([scene], {"f01": "5.5 小时"}) == []


def test_check_claims_missing_fact():
    from doc2video.contracts import Claim, ScriptScene

    scene = ScriptScene(
        id="sc01", chapter="一", narration=NARRATION, est_duration_s=13.3,
        source_block_ids=["b3"], claims=[Claim(fact_id="f99", quote="半衰期是五到六小时")],
    )
    problems = check_claims([scene], {"f01": "半衰期五到六小时"})
    assert any("f99" in p for p in problems)


def test_check_claims_quote_not_in_narration():
    from doc2video.contracts import Claim, ScriptScene

    scene = ScriptScene(
        id="sc01", chapter="一", narration=NARRATION, est_duration_s=13.3,
        source_block_ids=["b3"], claims=[Claim(fact_id="f01", quote="完全不相干的说法")],
    )
    problems = check_claims([scene], {"f01": "半衰期五到六小时"})
    assert any("不在旁白中" in p for p in problems)


# ── P2 / P3 阶段(FakeLLM) ──────────────────────────────────


def test_stage_p2(parsed_job: Path, monkeypatch):
    fake = FakeLLM(responses=_make_p2_responses())
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: fake)
    result = stage_p2(parsed_job, load_config(), None)
    assert isinstance(result, StageResult) and not result.needs_review
    doc = GroundedSummary.model_validate_json(
        (parsed_job / "grounded_summary.json").read_text(encoding="utf-8")
    )
    assert doc.facts and all(f.fact_id.startswith("f") for f in doc.facts)
    assert len({f.fact_id for f in doc.facts}) == len(doc.facts), "fact_id 必须全局唯一"
    assert doc.coverage.blocks_total > 0
    assert doc.chapter_plan  # 章节计划非空
    # 事实可追溯:source_block_ids 非空
    assert all(f.source_block_ids for f in doc.facts)


def test_stage_p2_token_count_failure_goes_needs_review(parsed_job: Path, monkeypatch):
    """S1.3b(H-04):token 计数失败 → needs_review(退出码 3),不再抛异常落入 failed。"""
    fake = FakeLLM(responses=_make_p2_responses())
    monkeypatch.setattr(fake, "count_tokens", lambda texts, allow_network=True: [-1] * len(texts))
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: fake)
    result = stage_p2(parsed_job, load_config(), None)
    assert result.needs_review, "计数失败应进审查通道,交人工处理"
    assert result.warnings
    assert not (parsed_job / "grounded_summary.json").exists(), "不得落盘半成品产物"


def test_stage_p3_ok(parsed_job: Path, monkeypatch):
    from doc2video.config import load_config as lc

    (parsed_job / "grounded_summary.json").write_text(
        _summary_json().model_dump_json(indent=2), encoding="utf-8"
    )
    scene_resp = {"scenes": [{
        "id": "sc01", "chapter": "概述", "narration": NARRATION, "est_duration_s": 13.3,
        "source_block_ids": ["b3"], "source_pages": [1],
        "claims": [{"fact_id": "f01", "quote": "半衰期是五到六小时"}],
    }]}
    fake = FakeLLM(responses={"【事实表】": scene_resp})
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: fake)
    result = stage_p3(parsed_job, lc(), None)
    assert not result.needs_review
    script = Script.model_validate_json((parsed_job / "script.json").read_text(encoding="utf-8"))
    assert script.scenes and script.scenes[0].claims


def test_stage_p3_needs_review_on_bad_claim(parsed_job: Path, monkeypatch):
    from doc2video.config import load_config as lc

    (parsed_job / "grounded_summary.json").write_text(
        _summary_json().model_dump_json(indent=2), encoding="utf-8"
    )
    bad_resp = {"scenes": [{
        "id": "sc01", "chapter": "概述", "narration": NARRATION, "est_duration_s": 13.3,
        "source_block_ids": ["b3"],
        "claims": [{"fact_id": "f99", "quote": "半衰期是五到六小时"}],  # f99 不存在
    }]}
    fake = FakeLLM(responses={"【事实表】": bad_resp})
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: fake)
    result = stage_p3(parsed_job, lc(), None)
    assert result.needs_review, "claim 引用不存在的 fact → needs_review(不静默通过)"


def _summary_json() -> GroundedSummary:
    from doc2video.contracts import (
        ChapterPlanItem,
        ChapterSummary,
        Coverage,
        Fact,
        FactNormalized,
    )

    return GroundedSummary(
        doc_summary="本文介绍咖啡因如何影响睡眠。",
        key_points=[],
        chapter_plan=[ChapterPlanItem(chapter="概述", section_ids=["s1", "s2"], planned_scenes=2)],
        chapter_summaries=[ChapterSummary(
            section_ids=["s1", "s2"], summary="介绍咖啡因机制与半衰期数据。",
            source_block_ids=["b3"],
        )],
        facts=[Fact(fact_id="f01", kind="number", text="半衰期五到六小时",
                    normalized=FactNormalized(value="5.5", unit="小时"),
                    source_block_ids=["b3"], source_pages=[1])],
        coverage=Coverage(blocks_seen=6, blocks_total=6),
    )
