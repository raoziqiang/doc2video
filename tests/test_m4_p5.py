"""M4/P5 TDD 测试:素材缓存、表格渲染、图片生成接缝、音频与 render timeline。"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from doc2video.cache import CacheStore, content_key
from doc2video.config import load_config
from doc2video.contracts import (
    AssetsManifest,
    RenderTimeline,
    SceneAssets,
)
from doc2video.pipeline.p5_assets import (
    compute_render_timeline,
    render_table_image,
    stage_p5,
)

from . import fixtures as fx


def _wav(path: Path, duration_s: float = 2.0) -> None:
    frames = int(16000 * duration_s)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\0\0" * frames)


def test_cache_put_get_is_content_addressed(tmp_path):
    cache = CacheStore(tmp_path / "cache")
    key = content_key("prompt", "model", "seed")
    src = tmp_path / "source.bin"
    src.write_bytes(b"asset-data")
    cache.put(key, src)
    hit = cache.get(key)
    assert hit is not None and hit.read_bytes() == b"asset-data"
    assert cache.get(content_key("prompt", "other-model", "seed")) is None


def test_cache_rejects_corrupted_entry(tmp_path):
    cache = CacheStore(tmp_path / "cache")
    key = content_key("x")
    src = tmp_path / "x.bin"
    src.write_bytes(b"good")
    cache.put(key, src)
    hit = cache.get(key)
    assert hit is not None
    hit.write_bytes(b"corrupt")
    assert cache.get(key) is None


def test_render_table_image_is_real_png(tmp_path):
    out = render_table_image(
        ["指标 | 数值", "半衰期 | 5.5 小时", "完全代谢 | 约 10 小时"],
        tmp_path / "table.png",
    )
    assert out.exists() and out.stat().st_size > 100
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_compute_render_timeline_uses_audio_duration():
    assets = AssetsManifest(scenes=[
        SceneAssets(scene_id="sc01", audio={"path": "a.wav", "duration_s": 2.0, "provider": "test"}),
        SceneAssets(scene_id="sc02", audio={"path": "b.wav", "duration_s": 3.5, "provider": "test"}),
    ])
    timeline = compute_render_timeline(assets, load_config())
    assert isinstance(timeline, RenderTimeline)
    assert timeline.scenes[0].scene_start_s == 0
    assert timeline.scenes[0].scene_total_s == pytest.approx(3.0)
    assert timeline.scenes[1].scene_start_s == pytest.approx(3.0)
    assert timeline.total_s == pytest.approx(7.5)


def test_stage_p5_cache_hit_avoids_second_image_call(tmp_path, monkeypatch):
    """生成式图片缓存命中:同 prompt/model/key 第二次不得调用 provider。"""
    cfg = load_config()
    job = tmp_path / "job"
    job.mkdir()
    src = fx.make_md(tmp_path)
    parsed = __import__("doc2video.pipeline.p1_parser", fromlist=["parse_document"]).parse_document(
        src, tmp_path, "md", cfg
    )
    (job / "parsed.json").write_text(parsed.model_dump_json(), encoding="utf-8")
    script = {
        "scenes": [{
            "id": "sc01", "chapter": "正文",
            "narration": "大家好,今天我们用五分钟解读这份文档的核心内容,先看整体结构,再看关键数据,最后给出实用建议帮助大家理解这些信息和结论。",
            "est_duration_s": 13.3, "source_block_ids": ["b1"], "source_pages": [1],
        }]
    }
    (job / "script.json").write_text(__import__("json").dumps(script, ensure_ascii=False), encoding="utf-8")
    scene_plan = {
        "style": {"name": "flat-illustration", "prefix": "扁平插画风格", "negative": "text"},
        "scenes": [{
            "id": "sc01", "chapter": "正文", "narration": script["scenes"][0]["narration"],
            "est_duration_s": 13.3, "visual_desc": "演讲者在屏幕前讲解核心数据,会议室环境,无文字。",
            "visual_source": "generated", "image_prompt": "咖啡杯与数据图形,无文字",
            "aspect": "16:9", "source_block_ids": ["b1"], "source_pages": [1],
        }],
    }
    (job / "scene_plan.json").write_text(__import__("json").dumps(scene_plan, ensure_ascii=False), encoding="utf-8")
    calls = []

    def fake_generate(prompt, out_path, cfg, privacy_mode):
        calls.append(prompt)
        out_path.write_bytes(b"fake-image")
        return {"provider": "fake", "model": "fake-v1", "request_id": "r1", "width": 16, "height": 9}

    monkeypatch.setattr("doc2video.pipeline.p5_assets.generate_image", fake_generate)
    def fake_audio(_text, out_path, _cfg, _privacy_mode):
        _wav(out_path, 2.0)
        return {"provider": "test", "voice": "test", "placeholder": False}

    monkeypatch.setattr("doc2video.pipeline.p5_assets.make_audio", fake_audio)
    r1 = stage_p5(job, cfg, type("Opts", (), {"privacy_mode": "approved_cloud"})())
    r2 = stage_p5(job, cfg, type("Opts", (), {"privacy_mode": "approved_cloud"})())
    assert r1.artifacts and r2.artifacts
    assert len(calls) == 1
    assets = AssetsManifest.model_validate_json((job / "assets_manifest.json").read_text(encoding="utf-8"))
    assert assets.scenes[0].image is not None and not assets.scenes[0].image.placeholder
