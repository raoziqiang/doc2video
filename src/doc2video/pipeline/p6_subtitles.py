"""P6 字幕:原生 TTS marks → faster-whisper 词级对齐 → 字符比例兜底。

所有 Cue 均转换到全片时间轴;P6 只读 P5 的 render_timeline,不重新计算场景起点。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import AssetsManifest, Cue, NativeMark, RenderTimeline, Script, Subtitles
from ..state import atomic_write_text
from .stages import StageResult

_PUNCTUATION = "。！？!?；;，,、：:"
_ASR_MODEL: Any = None


class SubtitleError(RuntimeError):
    pass


@dataclass(frozen=True)
class ASRWord:
    text: str
    start_s: float
    end_s: float
    probability: float = 1.0


def _format_caption(text: str, max_line_chars: int, max_lines: int) -> str:
    if max_lines < 1 or max_line_chars < 1:
        raise ValueError("字幕行配置必须为正数")
    max_chars = max_line_chars * max_lines
    if len(text) > max_chars:
        raise ValueError("单个字幕片段超过行数/字数上限")
    return "\n".join(text[i:i + max_line_chars] for i in range(0, len(text), max_line_chars))


def split_caption(text: str, max_line_chars: int = 18, max_lines: int = 2) -> list[str]:
    """确定性字幕切分:优先标点边界,每 Cue 最多两行、每行最多 18 字。"""
    text = text.strip()
    if not text:
        return []
    capacity = max_line_chars * max_lines
    result: list[str] = []
    rest = text
    while rest:
        if len(rest) <= capacity:
            result.append(_format_caption(rest, max_line_chars, max_lines))
            break
        window = rest[:capacity]
        cut = max((i + 1 for i, ch in enumerate(window) if ch in _PUNCTUATION), default=0)
        if cut < max(1, capacity // 2):
            cut = capacity
        piece = rest[:cut]
        result.append(_format_caption(piece, max_line_chars, max_lines))
        rest = rest[cut:]
    return result


def _cue_text_len(text: str) -> int:
    return len(text.replace("\n", ""))


def _time_allocate(parts: list[str], duration_s: float, start_s: float, source: str, scene_id: str) -> list[Cue]:
    if not parts or duration_s <= 0:
        return []
    total_chars = sum(max(1, _cue_text_len(p)) for p in parts)
    cues: list[Cue] = []
    cursor = start_s
    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            end = start_s + duration_s
        else:
            end = cursor + duration_s * max(1, _cue_text_len(part)) / total_chars
        cues.append(Cue(
            id=f"{scene_id}-cu{i + 1:03d}", scene_id=scene_id, text=part,
            start_s=cursor, end_s=end, source=source,
        ))
        cursor = end
    return cues


def build_fallback_cues(
    scene_id: str,
    text: str,
    scene_start_s: float,
    lead_s: float,
    audio_duration_s: float,
    cfg: dict[str, Any],
) -> list[Cue]:
    """无 ASR/marks 时按字符比例分配,时间范围严格落在 scene audio 区间。"""
    sub = cfg["subtitle"]
    parts = split_caption(text, int(sub["max_line_chars"]), int(sub["max_lines"]))
    # 最短显示时长不可满足时合并相邻片段,尽量保持 >= min_display_s。
    minimum = float(sub["min_display_s"])
    max_parts = max(1, int(audio_duration_s / minimum))
    while len(parts) > max_parts:
        merged: list[str] = []
        for i in range(0, len(parts), 2):
            joined = parts[i].replace("\n", "")
            if i + 1 < len(parts):
                joined += parts[i + 1].replace("\n", "")
            # 过长的合并仅影响可读性,不丢字符;时间门禁优先。
            merged.append(joined)
        parts = merged
    cues = _time_allocate(parts, audio_duration_s, scene_start_s + lead_s, "asr_fallback", scene_id)
    # 只有在物理上可满足时才报最小显示保证;否则返回可审计的最短可行结果。
    return cues


def align_cues_to_words(
    scene_id: str,
    text: str,
    words: list[ASRWord],
    audio_duration_s: float,
    lead_s: float,
    cfg: dict[str, Any] | None = None,
) -> list[Cue]:
    """把脚本文本片段映射到 ASR 词边界;输入/输出均为 scene-relative(含 lead)。"""
    if not words:
        return []
    cfg = cfg or {"subtitle": {"max_line_chars": 18, "max_lines": 2}}
    parts = split_caption(text, int(cfg["subtitle"]["max_line_chars"]), int(cfg["subtitle"]["max_lines"]))
    word_chars = [max(1, len(w.text)) for w in words]
    cumulative = [0]
    for n in word_chars:
        cumulative.append(cumulative[-1] + n)
    total_text = max(1, len(text.replace("\n", "")))
    total_words = cumulative[-1]
    cues: list[Cue] = []
    part_cursor = 0
    for i, part in enumerate(parts):
        part_len = _cue_text_len(part)
        target_start = round(part_cursor * total_words / total_text)
        target_end = round((part_cursor + part_len) * total_words / total_text)
        wi_start = min(len(words) - 1, next((j for j, n in enumerate(cumulative[1:]) if n > target_start), 0))
        wi_end = min(len(words) - 1, max(wi_start, next((j for j, n in enumerate(cumulative[1:]) if n >= target_end), len(words) - 1)))
        start = lead_s + max(0.0, words[wi_start].start_s)
        end = lead_s + min(audio_duration_s, max(words[wi_end].end_s, words[wi_start].end_s))
        if cues and start <= cues[-1].start_s:
            start = cues[-1].end_s
        if end <= start:
            end = start + min(0.01, max(0.01, audio_duration_s - (start - lead_s)))
        cues.append(Cue(
            id=f"{scene_id}-cu{i + 1:03d}", scene_id=scene_id, text=part,
            start_s=start, end_s=min(lead_s + audio_duration_s, end), source="aligned",
        ))
        part_cursor += part_len
    return cues


def _native_cues(scene_id: str, marks: list[NativeMark], scene_offset_s: float, audio_duration_s: float,
                 cfg: dict[str, Any]) -> list[Cue]:
    cues: list[Cue] = []
    seq = 0
    for mark in marks:
        for part in split_caption(mark.text, int(cfg["subtitle"]["max_line_chars"]), int(cfg["subtitle"]["max_lines"])):
            seq += 1
            # 同一个 mark 被拆分时按字符比例分配其原生边界。
            parts = split_caption(mark.text, int(cfg["subtitle"]["max_line_chars"]), int(cfg["subtitle"]["max_lines"]))
            idx = parts.index(part)
            total = sum(max(1, _cue_text_len(x)) for x in parts)
            rel_start = mark.start_s + (mark.end_s - mark.start_s) * sum(_cue_text_len(x) for x in parts[:idx]) / total
            rel_end = mark.start_s + (mark.end_s - mark.start_s) * sum(_cue_text_len(x) for x in parts[:idx + 1]) / total
            cues.append(Cue(
                id=f"{scene_id}-cu{seq:03d}", scene_id=scene_id, text=part,
                start_s=scene_offset_s + rel_start,
                end_s=min(scene_offset_s + audio_duration_s, scene_offset_s + rel_end), source="native",
            ))
    return cues


def transcribe_audio(path: Path, cfg: dict[str, Any] | None = None) -> list[ASRWord]:
    """CPU faster-whisper small 词级转写;模型惰性加载,失败由 stage 转比例兜底。"""
    global _ASR_MODEL
    from faster_whisper import WhisperModel

    if _ASR_MODEL is None:
        _ASR_MODEL = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _info = _ASR_MODEL.transcribe(
        str(path), language="zh", word_timestamps=True, vad_filter=True,
    )
    words: list[ASRWord] = []
    for segment in segments:
        if segment.words:
            words.extend(ASRWord(
                text=w.word.strip(), start_s=float(w.start), end_s=float(w.end),
                probability=float(getattr(w, "probability", 1.0)),
            ) for w in segment.words if w.word.strip())
        elif segment.text.strip():
            words.append(ASRWord(
                text=segment.text.strip(), start_s=float(segment.start), end_s=float(segment.end),
            ))
    return words


def stage_p6(job_dir: Path, cfg: dict[str, Any], opts: Any, stage: str | None = None) -> StageResult:
    script = Script.model_validate_json((job_dir / "script.json").read_text(encoding="utf-8"))
    assets = AssetsManifest.model_validate_json((job_dir / "assets_manifest.json").read_text(encoding="utf-8"))
    timeline = RenderTimeline.model_validate_json((job_dir / "render_timeline.json").read_text(encoding="utf-8"))
    assets_by_id = {s.scene_id: s for s in assets.scenes}
    timeline_by_id = {s.id: s for s in timeline.scenes}
    cues: list[Cue] = []
    warnings: list[str] = []
    for scene in script.scenes:
        asset = assets_by_id.get(scene.id)
        ts = timeline_by_id.get(scene.id)
        if asset is None or asset.audio is None or ts is None:
            raise SubtitleError(f"{scene.id}:缺少 P5 音频或 render timeline")
        audio_path = job_dir / asset.audio.path
        offset = ts.scene_start_s + ts.lead_s
        marks = asset.audio.native_marks
        if marks:
            scene_cues = _native_cues(scene.id, marks, offset, asset.audio.duration_s, cfg)
        else:
            try:
                words = transcribe_audio(audio_path, cfg)
            except Exception as exc:  # noqa: BLE001
                words = []
                warnings.append(f"{scene.id}: faster-whisper 失败,转字符比例兜底: {exc}")
            if words:
                local = align_cues_to_words(scene.id, scene.narration, words, asset.audio.duration_s, ts.lead_s, cfg)
                scene_cues = [c.model_copy(update={"start_s": c.start_s + ts.scene_start_s,
                                                    "end_s": c.end_s + ts.scene_start_s}) for c in local]
            else:
                scene_cues = build_fallback_cues(
                    scene.id, scene.narration, ts.scene_start_s, ts.lead_s, asset.audio.duration_s, cfg,
                )
                warnings.append(f"{scene.id}:无可用词级时间戳,使用 asr_fallback")
        if not scene_cues:
            raise SubtitleError(f"{scene.id}:未生成任何字幕 cue")
        cues.extend(scene_cues)
    subtitles = Subtitles(cues=cues)
    atomic_write_text(job_dir / "subtitles.json", subtitles.model_dump_json(indent=2) + "\n")
    return StageResult(
        artifacts=[("subtitles.json", "application/json")],
        warnings=warnings,
        needs_review=bool(warnings),
    )
