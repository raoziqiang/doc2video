"""P3 讲稿生成:分章生成口播稿 + claim→fact 一致性检查(方案 4.4)。

产物:script.json(每场景 source_block_ids/source_pages/claims,60–180 字,est_duration_s 按 4.5 字/秒)。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..contracts import GroundedSummary, ParsedDocument, Script, ScriptScene
from ..state import atomic_write_text
from .stages import StageResult

SYSTEM_PROMPT = "你是专业视频讲稿撰写人。只输出要求的 JSON。"
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "script_writer.txt"


def _ollama_schema(model_cls: type) -> dict[str, Any]:
    from ..contracts.common import to_schema

    s = to_schema(model_cls, "tmp")
    return {k: v for k, v in s.items() if k not in ("$schema", "$id")}


def _norm(text: str) -> str:
    """去掉空白与标点,用于 claim 包含性检查。"""
    return re.sub(r"[\s\W_]+", "", text)


_KEY_TOKEN_RE = re.compile(
    r"\d+(?:\.\d+)?%?|[零一二三四五六七八九十百千万两]+(?:年|月|日|小时|分钟|秒|%|％|元|亿|万|倍|号)?"
)


def _key_tokens(text: str) -> set[str]:
    """抽取数字/日期/单位类关键 token(方案 AC-04:数字/日期/专名/单位/否定 一致性检查)。"""
    return set(_KEY_TOKEN_RE.findall(text))


def check_claims(scenes: list[ScriptScene], facts: dict[str, str]) -> list[str]:
    """claim→fact 确定性一致性检查(方案 4.4/AC-04):返回问题列表。

    1. fact_id 必须存在;
    2. quote 必须逐字出现在旁白中(去空白标点,不允许 fact_id 前缀等污染);
    3. 事实传达:该事实的关键 token(数字/日期/单位)必须全部出现在旁白中
       —— 场景必须真的讲到了这个事实的数据;纯叙述事实则要求旁白整体包含事实文本。
    """
    problems: list[str] = []
    script_narration_norm = _norm("".join(scene.narration for scene in scenes))
    for scene in scenes:
        narration_norm = _norm(scene.narration)
        for claim in scene.claims:
            if claim.fact_id not in facts:
                problems.append(f"{scene.id}: claim 引用不存在的 fact {claim.fact_id}")
                continue
            fact_text = facts[claim.fact_id]
            quote_norm = _norm(claim.quote)
            fact_norm = _norm(fact_text)
            quote_tokens = _key_tokens(claim.quote)
            quote_ok = (
                not quote_norm
                or quote_norm in narration_norm
                or (quote_tokens and all(t in narration_norm for t in quote_tokens))
            )
            if not quote_ok:
                problems.append(f"{scene.id}: claim.quote 不在旁白中: {claim.quote!r}")
            fact_tokens = _key_tokens(fact_text)
            if fact_tokens:
                missing = [t for t in fact_tokens if _norm(t) not in script_narration_norm]
                if missing:
                    problems.append(
                        f"全脚本未传达事实 {claim.fact_id} 的关键数据: {missing}"
                    )
            elif fact_norm not in script_narration_norm:
                problems.append(
                    f"全脚本未传达事实 {claim.fact_id}(纯叙述事实): {fact_text!r}"
                )
    return problems


def _prompt_text() -> str:
    return _PROMPT_FILE.read_text(encoding="utf-8")


def _chapter_scenes(llm, chapter_plan, chapter_summaries, facts, blocks_by_section, cfg) -> list[ScriptScene]:
    """分章生成:每章注入章节摘要/事实表/相关原文块(禁止只依赖总摘要)。"""
    facts_by_block: dict[str, list] = {}
    for f in facts:
        for bid in f.source_block_ids:
            facts_by_block.setdefault(bid, []).append(f)

    all_scenes: list[ScriptScene] = []
    template = _prompt_text()
    for plan, ch_sum in zip(chapter_plan, chapter_summaries):
        section_ids = set(plan.section_ids)
        src_parts: list[str] = []
        chapter_facts: list[str] = []
        chapter_pages: set[int] = set()
        for sid in section_ids:
            for bid, text, page in blocks_by_section.get(sid, []):
                src_parts.append(f"[{bid}] {text}")
                if page:
                    chapter_pages.add(page)
                for f in facts_by_block.get(bid, []):
                    chapter_facts.append(f"[{f.fact_id}] {f.text}")
        user = (
            template.replace("{max_scenes}", str(plan.planned_scenes))
            .replace("{chapter}", plan.chapter)
            .replace("{chapter_summaries}", ch_sum.summary)
            .replace("{facts}", "\n".join(chapter_facts) or "(无)")
            .replace("{source_blocks}", "\n".join(src_parts))
        )
        chapter_script = llm.complete_json(
            SYSTEM_PROMPT, user, Script, json_schema=_ollama_schema(Script)
        )
        all_scenes.extend(chapter_script.scenes)
    return all_scenes


def stage_p3(job_dir: Path, cfg: dict[str, Any], opts: Any, stage: str | None = None) -> StageResult:
    from ..providers import build_llm

    parsed = ParsedDocument.model_validate_json((job_dir / "parsed.json").read_text(encoding="utf-8"))
    summary = GroundedSummary.model_validate_json(
        (job_dir / "grounded_summary.json").read_text(encoding="utf-8")
    )
    llm = build_llm(cfg)

    blocks_by_section: dict[str, list[tuple[str, str, int | None]]] = {}
    for s in parsed.sections:
        for b in s.blocks:
            if b.text.strip():
                blocks_by_section.setdefault(s.id, []).append((b.block_id, b.text, b.page))

    scenes = _chapter_scenes(
        llm, summary.chapter_plan, summary.chapter_summaries, summary.facts,
        blocks_by_section, cfg,
    )

    facts_map = {f.fact_id: f.text for f in summary.facts}
    problems = check_claims(scenes, facts_map)
    if problems:
        # 修复重试一次:把问题反馈给 LLM;LLM 报错/仍不合格 → 保留原问题进入 needs_review
        try:
            retry_user = (
                "上一版讲稿的 claim 一致性校验失败,请基于下面的完整输入逐条修复。\n"
                "【失败清单】\n"
                + "\n".join(problems[:10])
                + "\n【事实表】\n"
                + str(facts_map)
                + "\n【上一版完整讲稿 JSON】\n"
                + Script(scenes=scenes).model_dump_json(ensure_ascii=False)
                + "\n处理规则:"
                "\n- 若问题为'全脚本未传达事实 X 的关键数据':把该数据(如 5.5 小时)写进引用该事实的场景 narration,"
                "并保证 narration 仍为 60–180 字;"
                "\n- 若问题为 'claim 引用不存在的 fact' 或 'quote 不在旁白中':删除该 claim,"
                "或把 quote 改为 narration 中的连续片段;"
                "\n- 不得改变其余场景的事实含义、来源块和章节。\n"
                "只输出修复后的全部场景 JSON。"
            )
            retry = llm.complete_json(SYSTEM_PROMPT, retry_user, Script, json_schema=_ollama_schema(Script))
            scenes = retry.scenes
            problems = check_claims(scenes, facts_map)
        except Exception:  # noqa: BLE001, S110 —— 修复失败按原问题 needs_review,不崩
            pass

    # id 全局重排(跨章合并后保证 sc01.. 顺序唯一)
    renumbered: list[ScriptScene] = []
    for i, s in enumerate(scenes, start=1):
        renumbered.append(s.model_copy(update={"id": f"sc{i:02d}"}))
    script = Script(scenes=renumbered)
    atomic_write_text(job_dir / "script.json", script.model_dump_json(indent=2) + "\n")

    if problems:
        # 重试后仍不合格 → needs_review(不静默通过;退出码 3,产物照常落盘供人工检查)
        return StageResult(
            artifacts=[("script.json", "application/json")],
            warnings=problems,
            needs_review=True,
        )
    return StageResult(artifacts=[("script.json", "application/json")])
