"""M6/P7 TDD 测试:真实 ffmpeg 合成、ASS 字幕、loudnorm 与 sidechain 方向。"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from doc2video.config import load_config
from doc2video.contracts import (
    AssetsManifest,
    Cue,
    ImageAsset,
    RenderTimeline,
    SceneAssets,
    ScenePlan,
    Subtitles,
)
from doc2video.pipeline.p7_render import (
    build_ass,
    build_audio_mix_filter,
    probe_media,
    stage_p7,
)


def _png(path: Path, size=(640, 360)) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (20, 50, 100))
    ImageDraw.Draw(img).rectangle((40, 40, size[0] - 40, size[1] - 40), outline=(240, 140, 20), width=8)
    img.save(path)


def _wav(path: Path, duration=2.0, freq=440) -> None:
    import math
    import struct

    rate = 48000
    frames = bytearray()
    for i in range(int(rate * duration)):
        value = int(8000 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)


def _stage_inputs(tmp_path: Path) -> Path:
    job = tmp_path / "job"
    (job / "assets").mkdir(parents=True)
    image = job / "assets" / "sc01.png"
    audio = job / "assets" / "sc01.wav"
    _png(image)
    _wav(audio)
    narration = "大家好,今天我们介绍这份文档的核心内容,再看关键数据和最后的实用建议,帮助大家完整理解这份材料并掌握重点信息。这是一段用于验证视频渲染流程的示例旁白。"
    (job / "scene_plan.json").write_text(ScenePlan(
        style={"name": "flat-illustration", "prefix": "扁平插画", "negative": "text"},
        scenes=[{
            "id": "sc01", "chapter": "一", "narration": narration, "est_duration_s": 13.3,
            "visual_desc": "蓝色会议室中演讲者讲解资料,无文字。", "visual_source": "generated",
            "image_prompt": "演讲者讲解资料,无文字", "aspect": "16:9",
            "source_block_ids": ["b1"], "source_pages": [1],
        }],
    ).model_dump_json(), encoding="utf-8")
    (job / "assets_manifest.json").write_text(AssetsManifest(scenes=[
        SceneAssets(scene_id="sc01", image=ImageAsset(
            path="assets/sc01.png", cache_key="a" * 64, provider="test", width=640, height=360,
        ), audio={"path": "assets/sc01.wav", "duration_s": 2.0, "provider": "test"}),
    ]).model_dump_json(), encoding="utf-8")
    (job / "render_timeline.json").write_text(RenderTimeline(scenes=[{
        "id": "sc01", "scene_start_s": 0.0, "lead_s": 0.2, "audio_duration_s": 2.0,
        "trail_s": 0.2, "scene_total_s": 2.4, "fade_out_start_s": 2.1,
    }], total_s=2.4).model_dump_json(), encoding="utf-8")
    (job / "subtitles.json").write_text(Subtitles(cues=[
        Cue(id="sc01-cu001", scene_id="sc01", text="大家好,今天我们介绍\n这份文档的核心内容。",
            start_s=0.2, end_s=1.2, source="aligned"),
        Cue(id="sc01-cu002", scene_id="sc01", text="再看关键数据和建议。",
            start_s=1.2, end_s=2.2, source="aligned"),
    ]).model_dump_json(), encoding="utf-8")
    return job


def test_build_ass_escapes_cjk_and_uses_global_times():
    subs = Subtitles(cues=[Cue(
        id="sc01-cu001", scene_id="sc01", text="大家好\n核心内容", start_s=0.2, end_s=1.2, source="aligned",
    )])
    ass = build_ass(subs)
    assert "Microsoft YaHei" in ass
    assert "0:00:00.20,0:00:01.20" in ass
    assert "大家好\\N核心内容" in ass


def test_audio_mix_filter_sidechains_bgm_not_narration():
    filt = build_audio_mix_filter(narration_input="[0:a]", bgm_input="[1:a]", delay_ms=200, duration_s=2.4)
    assert "[bgm_delayed][narration_sc]sidechaincompress" in filt
    assert "[narration_delayed][bgm_duck]amix" in filt
    assert "[narration_sc][bgm_delayed]sidechaincompress" not in filt
    assert "adelay=200|200" in filt


def test_probe_media_reads_real_streams(tmp_path):
    p = tmp_path / "x.wav"
    _wav(p, 0.5)
    info = probe_media(p)
    assert info["format"]["duration"] > 0.4
    assert any(s.get("codec_name") == "pcm_s16le" for s in info["streams"])


def test_stage_p7_real_ffmpeg_mp4_and_subtitles(tmp_path: Path):
    job = _stage_inputs(tmp_path)
    result = stage_p7(job, load_config(), type("Opts", (), {})())
    assert not result.needs_review
    assert (job / "render" / "staging.mp4").exists()
    assert (job / "render" / "final.mp4").exists()
    manifest = json.loads((job / "render_manifest.json").read_text(encoding="utf-8"))
    assert manifest["command_argv"] and manifest["command_argv"][0] == "ffmpeg"
    assert "-vf" in manifest["command_argv"]
    assert manifest["timeline_ref_sha256"]
    info = probe_media(job / "render" / "final.mp4")
    video = next(s for s in info["streams"] if s.get("codec_type") == "video")
    audio = next(s for s in info["streams"] if s.get("codec_type") == "audio")
    assert (video["codec_name"], video["width"], video["height"], video["pix_fmt"]) == (
        "h264", 1920, 1080, "yuv420p"
    )
    assert audio["codec_name"] == "aac"
    assert float(info["format"]["duration"]) == pytest.approx(2.4, abs=0.15)
