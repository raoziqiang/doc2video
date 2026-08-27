"""M5/P6 TDD 测试:字幕切行、时间边界、native/ASR/比例兜底与全片偏移。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from doc2video.config import load_config
from doc2video.contracts import AssetsManifest, RenderTimeline, Subtitles
from doc2video.pipeline.p6_subtitles import (
    ASRWord,
    build_fallback_cues,
    split_caption,
    stage_p6,
)


def test_split_caption_max_two_lines_and_chars():
    text = "这是一个用于测试字幕切分的中文句子,它需要被拆成不超过两行且每行不超过十八个字符。"
    parts = split_caption(text, max_line_chars=18, max_lines=2)
    assert parts
    for part in parts:
        lines = part.split("\n")
        assert len(lines) <= 2
        assert all(len(line) <= 18 for line in lines)
        assert len("".join(lines)) <= 36
    assert "".join(p.replace("\n", "") for p in parts) == text


def test_split_caption_prefers_punctuation_boundary():
    parts = split_caption("第一句讲完了。第二句继续说明,最后收束。", max_line_chars=18, max_lines=2)
    assert parts[0].endswith("。")


def test_fallback_cues_have_global_offset_and_min_duration():
    cues = build_fallback_cues(
        scene_id="sc01", text="这是一段足够长的字幕内容,需要按时间比例分配到多个字幕块中,并且继续补充更多说明来覆盖时间轴。",
        scene_start_s=10.0, lead_s=0.5, audio_duration_s=5.0, cfg=load_config(),
    )
    assert len(cues) >= 2
    assert cues[0].start_s == pytest.approx(10.5)
    assert all(c.end_s > c.start_s for c in cues)
    assert all(c.end_s - c.start_s >= 0.8 - 1e-6 for c in cues)
    assert all(c.start_s >= 10.5 and c.end_s <= 15.5 + 1e-6 for c in cues)
    assert all(c.source == "asr_fallback" for c in cues)


def test_word_alignment_source_and_monotonic_boundaries():
    words = [
        ASRWord(text="这是", start_s=0.0, end_s=0.5, probability=0.95),
        ASRWord(text="一段", start_s=0.5, end_s=1.0, probability=0.92),
        ASRWord(text="字幕", start_s=1.0, end_s=1.5, probability=0.90),
        ASRWord(text="内容", start_s=1.5, end_s=2.0, probability=0.88),
    ]
    from doc2video.pipeline.p6_subtitles import align_cues_to_words

    cues = align_cues_to_words("sc01", "这是一段字幕内容", words, 20.0, 0.5)
    assert cues
    assert all(c.source == "aligned" for c in cues)
    assert all(c.end_s > c.start_s for c in cues)
    assert cues[0].start_s >= 0.5
    assert cues[-1].end_s <= 2.5 + 1e-6


def test_stage_p6_native_marks_and_schema(tmp_path: Path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    script = {
        "scenes": [{
            "id": "sc01", "chapter": "一",
            "narration": "大家好,今天我们用五分钟介绍这份文档的核心内容,再看关键数据和最后的实用建议,帮助大家完整理解这份材料。这段文字用于测试字幕对齐和时间边界。",
            "est_duration_s": 13.3, "source_block_ids": ["b1"], "source_pages": [1],
        }]
    }
    (job / "script.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    assets = AssetsManifest(scenes=[{
        "scene_id": "sc01",
        "audio": {
            "path": "assets/sc01.wav", "duration_s": 4.0, "provider": "test",
            "native_marks": [
                {"text": "大家好", "start_s": 0.0, "end_s": 0.8},
                {"text": "今天我们介绍", "start_s": 0.8, "end_s": 2.0},
                {"text": "核心内容", "start_s": 2.0, "end_s": 3.0},
                {"text": "实用建议", "start_s": 3.0, "end_s": 4.0},
            ],
        },
        "image": None,
    }])
    timeline = RenderTimeline(scenes=[{
        "id": "sc01", "scene_start_s": 5.0, "lead_s": 0.5,
        "audio_duration_s": 4.0, "trail_s": 0.5, "scene_total_s": 5.0,
        "fade_out_start_s": 9.7,
    }], total_s=10.0)
    (job / "assets_manifest.json").write_text(assets.model_dump_json(), encoding="utf-8")
    (job / "render_timeline.json").write_text(timeline.model_dump_json(), encoding="utf-8")
    result = stage_p6(job, load_config(), None)
    assert not result.needs_review
    subtitles = Subtitles.model_validate_json((job / "subtitles.json").read_text(encoding="utf-8"))
    assert subtitles.cues and all(c.source == "native" for c in subtitles.cues)
    assert subtitles.cues[0].start_s >= 5.5
    assert subtitles.cues[-1].end_s <= 9.5 + 1e-6


def test_stage_p6_no_words_falls_back_and_requests_review(tmp_path: Path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    script = {"scenes": [{
        "id": "sc01", "chapter": "一",
        "narration": "大家好,今天我们用五分钟介绍这份文档的核心内容,再看关键数据和最后的实用建议,帮助大家完整理解这份材料。这段文字用于测试字幕对齐和时间边界。",
        "est_duration_s": 13.3, "source_block_ids": ["b1"], "source_pages": [1],
    }]}
    (job / "script.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    assets = AssetsManifest(scenes=[{
        "scene_id": "sc01", "audio": {"path": "assets/a.wav", "duration_s": 4.0, "provider": "test"},
    }])
    timeline = RenderTimeline(scenes=[{
        "id": "sc01", "scene_start_s": 0.0, "lead_s": 0.5,
        "audio_duration_s": 4.0, "trail_s": 0.5, "scene_total_s": 5.0, "fade_out_start_s": 4.7,
    }], total_s=5.0)
    (job / "assets_manifest.json").write_text(assets.model_dump_json(), encoding="utf-8")
    (job / "render_timeline.json").write_text(timeline.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("doc2video.pipeline.p6_subtitles.transcribe_audio", lambda *a, **k: [])
    result = stage_p6(job, load_config(), None)
    assert result.needs_review
    subtitles = Subtitles.model_validate_json((job / "subtitles.json").read_text(encoding="utf-8"))
    assert subtitles.cues and all(c.source == "asr_fallback" for c in subtitles.cues)
