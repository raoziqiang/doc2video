"""P2 内容理解:tokenizer 实测切块 → 逐块摘要与事实抽取(map)→ 归并(reduce)。

产物:grounded_summary.json(doc_summary / key_points / chapter_plan / chapter_summaries /
facts[fact_id + 规范化值 + source_block_ids] / coverage)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..contracts import (
    ChapterPlanItem,
    ChapterSummary,
    Contract,
    Coverage,
    Fact,
    FactNormalized,
    GroundedSummary,
    KeyPoint,
    ParsedDocument,
)
from ..state import atomic_write_text
from .stages import StageResult

SYSTEM_PROMPT = "你是严谨的文档分析器。只输出要求的 JSON,不得编造原文不存在的信息。"


# ── 本地中间契约(不落盘,仅约束 LLM 输出) ────────────────────


class _FactOut(Contract):
    kind: Literal["number", "date", "proper_noun", "unit", "negation"]
    text: str = Field(min_length=1)
    normalized: FactNormalized = Field(default_factory=FactNormalized)


class _ChunkItem(Contract):
    block_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    facts: list[_FactOut] = Field(default_factory=list)
    low_info: bool = False


class _ChunkResults(Contract):
    results: list[_ChunkItem] = Field(min_length=1)


class _ReduceOut(Contract):
    doc_summary: str = Field(min_length=10)
    key_points: list[KeyPoint] = Field(default_factory=list)


# ── Prompt 模板 ─────────────────────────────────────────────

_CHUNK_PROMPT = """对下面 JSON 数组中的每个【块】输出分析结果。规则:
1. summary:该块 2–4 句摘要,只依据块内原文,保留具体名词;
2. facts:只抽取数字、日期、专有名词、单位、否定关系,text 必须逐字照抄原文短语(保留具体名词,不得改写、不得用指代词替换);
3. low_info:目录、过渡句等低信息块标 true(不要标页眉页脚——它们已被解析层剔除);
4. 输出 JSON:{"results":[{"block_id":"…","summary":"…","facts":[{"kind":"number","text":"原文短语","normalized":{"value":…,"unit":null,"polarity":"positive"}}],"low_info":false}]}

【块列表】
{blocks_json}"""

_REDUCE_PROMPT = """根据【分块摘要】与【要点候选】输出总览 JSON:
{"doc_summary":"150–300 字,覆盖各章要点","key_points":[{"text":"要点","source_block_ids":["b…"]}]}
【分块摘要】
{chunk_summaries}
【要点候选】
{facts_text}"""


def _ollama_schema(model_cls: type) -> dict[str, Any]:
    from ..contracts.common import to_schema

    s = to_schema(model_cls, "tmp")
    return {k: v for k, v in s.items() if k not in ("$schema", "$id")}


def _input_budget(cfg: dict[str, Any]) -> int:
    c = cfg["llm"]
    return int(c["num_ctx"] * (1 - c["budget_reserve_ratio"])) - c["max_output_tokens"]


def _chunk_blocks(blocks: list[tuple[str, str]], token_counts: list[int], budget: int) -> list[list[tuple[str, str]]]:
    """按实测 token 数切块;单块超预算 → 硬切(按预算比例,不静默丢弃)。"""
    chunks: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    cur_tokens = 0
    for (bid, text), n in zip(blocks, token_counts):
        if n > budget:
            # 单块超预算:按字符比例切碎(确定性)
            piece_chars = max(1, int(len(text) * budget / n))
            if cur:
                chunks.append(cur)
                cur, cur_tokens = [], 0
            for i in range(0, len(text), piece_chars):
                chunks.append([(f"{bid}#{i // piece_chars}", text[i:i + piece_chars])])
            continue
        if cur_tokens + n > budget and cur:
            chunks.append(cur)
            cur, cur_tokens = [], 0
        cur.append((bid, text))
        cur_tokens += n
    if cur:
        chunks.append(cur)
    return chunks


def _build_chapter_plan(parsed: ParsedDocument, cfg: dict[str, Any]) -> list[ChapterPlanItem]:
    chars_per_scene = cfg["video"]["chars_per_scene_plan"]  # 500 字/场景粗估
    max_scenes = cfg["video"]["max_scenes"]
    top = [s for s in parsed.sections if s.level == 1 and any(b.text.strip() for b in s.blocks)]
    if top:
        chapters: list[tuple[str, list[str], int]] = []
        cur_id: list[str] = []
        cur_name = ""
        cur_chars = 0
        for s in parsed.sections:
            if not any(b.text.strip() for b in s.blocks):
                continue  # 空节(如无标题文档的占位节)不进计划
            if s.level == 1:
                if cur_id:
                    chapters.append((cur_name, cur_id, cur_chars))
                cur_name, cur_id, cur_chars = s.heading, [], 0
            cur_id.append(s.id)
            cur_chars += sum(len(b.text) for b in s.blocks)
        if cur_id:
            chapters.append((cur_name, cur_id, cur_chars))
    else:
        chapters = [(
            parsed.title or "正文",
            [s.id for s in parsed.sections if any(b.text.strip() for b in s.blocks)],
            sum(len(b.text) for s in parsed.sections for b in s.blocks),
        )]
    chapters = [(n, ids, c) for n, ids, c in chapters if ids and c > 0]
    raw = [max(2, round(chars / chars_per_scene)) for _, _, chars in chapters]
    total = sum(raw)
    if total > max_scenes:  # 等比例压缩,每章至少 1
        scale = max_scenes / total
        raw = [max(1, round(r * scale)) for r in raw]
    return [
        ChapterPlanItem(chapter=name, section_ids=ids, planned_scenes=max(1, n))
        for (name, ids, _), n in zip(chapters, raw)
    ]


def stage_p2(job_dir: Path, cfg: dict[str, Any], opts: Any, stage: str | None = None) -> StageResult:
    from ..providers import build_llm

    parsed = ParsedDocument.model_validate_json((job_dir / "parsed.json").read_text(encoding="utf-8"))
    llm = build_llm(cfg)

    blocks = [(b.block_id, b.text) for s in parsed.sections for b in s.blocks if b.text.strip()]
    if not blocks:
        raise ValueError("REJECT_MALFORMED: parsed.json 无可分析文本块")

    token_counts = llm.count_tokens([t for _, t in blocks])
    if any(n < 0 for n in token_counts):
        raise ValueError("NEEDS_REVIEW: token 计数失败(LLM 服务异常)")
    budget = _input_budget(cfg)
    chunks = _chunk_blocks(blocks, token_counts, budget)

    all_items: dict[str, _ChunkItem] = {}
    for chunk in chunks:
        blocks_json = json.dumps(
            [{"block_id": bid, "text": text} for bid, text in chunk],
            ensure_ascii=False,
        )
        user = _CHUNK_PROMPT.replace("{blocks_json}", blocks_json)
        out = llm.complete_json(SYSTEM_PROMPT, user, _ChunkResults, json_schema=_ollama_schema(_ChunkResults))
        for item in out.results:
            all_items[item.block_id] = item

    # reduce:总摘要 + 要点
    chunk_summaries = "\n".join(f"- [{bid}] {it.summary}" for bid, it in all_items.items() if not it.low_info)
    facts_text = "\n".join(f"- {f.text}" for it in all_items.values() for f in it.facts[:20])
    reduce_user = _REDUCE_PROMPT.replace("{chunk_summaries}", chunk_summaries or "(无)").replace(
        "{facts_text}", facts_text or "(无)"
    )
    reduce_out = llm.complete_json(SYSTEM_PROMPT, reduce_user, _ReduceOut, json_schema=_ollama_schema(_ReduceOut))

    # facts 归并:全局唯一编号 f01..(按事实计数,而非按块)
    facts: list[Fact] = []
    fact_counter = 0
    for it in all_items.values():
        for f in it.facts:
            fact_counter += 1
            base_id = it.block_id.lstrip("b").split("#")[0]
            facts.append(Fact(
                fact_id=f"f{fact_counter:02d}",
                kind=f.kind,
                text=f.text,
                normalized=f.normalized,
                source_block_ids=[f"b{base_id}"],
            ))

    chapter_plan = _build_chapter_plan(parsed, cfg)
    section_blocks: dict[str, list[str]] = {
        s.id: [b.block_id for b in s.blocks if b.block_id in all_items] for s in parsed.sections
    }
    fallback_bid = next(iter(all_items), None)
    chapter_summaries = [
        ChapterSummary(
            section_ids=item.section_ids,
            summary=";".join(
                it.summary for sid in item.section_ids
                for bid in section_blocks.get(sid, [])
                if (it := all_items.get(bid)) is not None and not it.low_info
            ) or "本章内容",
            source_block_ids=(
                [bid for sid in item.section_ids for bid in section_blocks.get(sid, [])]
                or ([fallback_bid] if fallback_bid else [])
            ),
        )
        for item in chapter_plan
    ]

    seen_ids = set(all_items)
    must_cover = {bid for bid, _ in blocks}
    uncovered = sorted(must_cover - seen_ids)

    doc = GroundedSummary(
        doc_summary=reduce_out.doc_summary,
        key_points=reduce_out.key_points,
        chapter_plan=chapter_plan,
        chapter_summaries=chapter_summaries,
        facts=facts,
        coverage=Coverage(blocks_seen=len(seen_ids), blocks_total=len(must_cover),
                          uncovered_block_ids=uncovered),
    )
    atomic_write_text(job_dir / "grounded_summary.json", doc.model_dump_json(indent=2) + "\n")
    warnings = [f"{len(uncovered)} 个块未获得摘要" ] if uncovered else []
    return StageResult(artifacts=[("grounded_summary.json", "application/json")], warnings=warnings)
