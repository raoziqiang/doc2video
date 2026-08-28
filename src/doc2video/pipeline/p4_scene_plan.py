"""P4 分镜规划:讲稿场景 → 可追溯画面计划 + style bible。

LLM 只负责画面语义描述;视觉来源、来源引用和风格由确定性规则约束,避免模型
把原文图表误生成或引用不存在的 block。产物:scene_plan.json。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from ..contracts import Contract, ParsedDocument, ScenePlan, ScenePlanScene, Script, StyleTemplate
from ..state import atomic_write_text
from .stages import StageResult

SYSTEM_PROMPT = "你是专业分镜设计师。只输出 JSON,不得编造文档中不存在的视觉事实。"


class _VisualScene(Contract):
    """P4 LLM 最小输出契约:讲稿和来源字段全部由程序回填,不让模型重复生成。"""

    id: str = Field(pattern=r"^sc\d{2,3}$")
    visual_desc: str = Field(min_length=10)
    image_prompt: str | None = None


class _ScenePlanOutput(Contract):
    scenes: list[_VisualScene] = Field(min_length=1, max_length=20)


def _schema(model_cls: type[Contract]) -> dict[str, Any]:
    from ..contracts.common import to_schema

    raw = to_schema(model_cls, "p4")
    return {k: v for k, v in raw.items() if k not in ("$schema", "$id")}


def load_style_template(cfg: dict[str, Any], name: str) -> StyleTemplate:
    """读取并通过 Pydantic 校验 style bible;未知风格 fail closed。"""
    config_dir = Path(__file__).resolve().parents[3] / "config"
    path = config_dir / "styles.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if name not in data:
        raise ValueError(f"未知 style: {name}")
    item = data[name]
    return StyleTemplate(name=name, prefix=item["prefix"], negative=item["negative"])


def choose_visual_source(
    block_types: dict[str, str],
    source_block_ids: list[str],
    extracted_refs: dict[str, str],
) -> tuple[str, str | None]:
    """确定性选择画面来源:原文图片 > 表格 > 概念生成。

    返回值符合 ScenePlanScene 的 visual_source/extracted_ref 条件约束。
    """
    for bid in source_block_ids:
        if block_types.get(bid) == "image" and bid in extracted_refs:
            return "extracted_image", extracted_refs[bid]
    if any(block_types.get(bid) == "table" for bid in source_block_ids):
        # 派生文件由后续 P5 依据同一 block 生成;此处只冻结寻址名。
        # 必须取真正的表格块:第一个引用块可能是段落,取 [0] 会冻结错误地址。
        table_bid = next(bid for bid in source_block_ids if block_types.get(bid) == "table")
        return "rendered_table", f"derived/table-{table_bid}.png"
    return "generated", None


def _source_payload(parsed: ParsedDocument, script: Script) -> tuple[dict[str, str], dict[str, str], dict[str, list[int]]]:
    block_types: dict[str, str] = {}
    extracted_refs: dict[str, str] = {}
    pages: dict[str, list[int]] = {}
    for section in parsed.sections:
        for block in section.blocks:
            block_types[block.block_id] = block.type
            pages[block.block_id] = [block.page] if block.page else []
            if block.type == "image" and block.text:
                extracted_refs[block.block_id] = block.text
    return block_types, extracted_refs, pages


def _prompt(parsed: ParsedDocument, script: Script, style: StyleTemplate) -> str:
    scenes = []
    blocks = {b.block_id: b for s in parsed.sections for b in s.blocks}
    for sc in script.scenes:
        refs = [
            {"block_id": bid, "type": blocks[bid].type, "text": blocks[bid].text[:500]}
            for bid in sc.source_block_ids if bid in blocks
        ]
        scenes.append({
            "scene_id": sc.id, "chapter": sc.chapter, "narration": sc.narration,
            "source_block_ids": sc.source_block_ids, "source_pages": sc.source_pages,
            "source_blocks": refs,
        })
    return (
        "【分镜输入】\n"
        + json.dumps({
            "style": style.model_dump(),
            "rules": [
                "每个输入场景必须输出一个同 id 场景,不可增删",
                "只输出 id、visual_desc、image_prompt 三个字段;讲稿、时长、来源引用由程序回填",
                "visual_desc 至少描述主体、动作、环境,禁止画面文字",
                "概念场景必须提供 image_prompt; prompt 只描述与原文相关的画面",
            ],
            "scenes": scenes,
        }, ensure_ascii=False)
    )


def stage_p4(job_dir: Path, cfg: dict[str, Any], opts: Any, stage: str | None = None) -> StageResult:
    from ..providers import build_llm

    parsed = ParsedDocument.model_validate_json((job_dir / "parsed.json").read_text(encoding="utf-8"))
    script = Script.model_validate_json((job_dir / "script.json").read_text(encoding="utf-8"))
    style = load_style_template(cfg, getattr(opts, "style", "flat-illustration"))
    block_types, extracted_refs, pages = _source_payload(parsed, script)
    llm = build_llm(cfg)
    output = llm.complete_json(
        SYSTEM_PROMPT, _prompt(parsed, script, style), _ScenePlanOutput,
        json_schema=_schema(_ScenePlanOutput),
    )

    script_by_id = {s.id: s for s in script.scenes}
    output_by_id = {s.id: s for s in output.scenes}
    warnings: list[str] = []
    normalized: list[ScenePlanScene] = []
    for sid, src in script_by_id.items():
        candidate = output_by_id.get(sid)
        if candidate is None:
            warnings.append(f"缺少讲稿场景 {sid} 的分镜")
            continue
        # 讲稿引用是唯一来源真相,不接受 LLM 擅自换引用。
        source_ids = list(src.source_block_ids)
        unknown = [bid for bid in source_ids if bid not in block_types]
        if unknown:
            warnings.append(f"{sid}:引用不存在的 source_block_ids: {unknown}")
            continue
        # 讲稿引用是唯一来源真相,不接受 LLM 擅自换引用。
        visual_source, extracted_ref = choose_visual_source(block_types, source_ids, extracted_refs)
        image_prompt = candidate.image_prompt
        if visual_source == "generated":
            if not image_prompt:
                warnings.append(f"{sid}:generated 缺少 image_prompt")
                continue
            image_prompt = f"{style.prefix};{image_prompt};{style.negative}"
        else:
            image_prompt = None
        normalized.append(ScenePlanScene(
            id=src.id,
            chapter=src.chapter,
            narration=src.narration,
            est_duration_s=src.est_duration_s,
            visual_desc=candidate.visual_desc,
            visual_source=visual_source,
            image_prompt=image_prompt,
            extracted_ref=extracted_ref,
            aspect=getattr(opts, "aspect", "16:9"),
            source_block_ids=source_ids,
            source_pages=src.source_pages or [p for bid in source_ids for p in pages.get(bid, [])],
        ))
    extra = sorted(set(output_by_id) - set(script_by_id))
    if extra:
        warnings.append(f"模型输出了未请求的场景: {extra}")
    if len(normalized) != len(script.scenes):
        warnings.append(f"分镜数量不完整: {len(normalized)}/{len(script.scenes)}")

    if not normalized:
        return StageResult(
            artifacts=[],
            warnings=warnings or ["P4 未生成任何可用分镜"],
            needs_review=True,
        )
    plan = ScenePlan(style=style, scenes=normalized)
    atomic_write_text(job_dir / "scene_plan.json", plan.model_dump_json(indent=2) + "\n")
    return StageResult(
        artifacts=[("scene_plan.json", "application/json")],
        warnings=warnings,
        needs_review=bool(warnings),
    )
