"""S2.4 端到端冒烟(AC-9):小输入 → 全管线 → 断言最终成片含视频/音频/字幕、时长与响度达标。

真实部分:ffmpeg 渲染、P8 全量媒体复核;替身部分(测试不外发):LLM/TTS/图片生成。
fake TTS 必须返回 raw_marks 走 marks 主路径,避免 whisper 兜底警告阻断下游。
"""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

from doc2video.cli import main
from doc2video.state import TERMINAL_OK, StateStore

from .fake_llm import FakeLLM

_FAKE_RESPONSES = {
    "【块列表】": {
        "results": [{"block_id": "b1", "summary": "正文内容摘要。", "facts": [], "low_info": False}]
    },
    "【分块摘要】": {
        "doc_summary": "这是演示文档的总摘要,内容简短,用于测试端到端流水线。",
        "key_points": [],
    },
    "【事实表】": {
        "scenes": [{
            "id": "sc01", "chapter": "咖啡因与睡眠",
            "narration": "大家好,今天我们用五分钟解读这份报告的核心结论,先看它的整体框架与关键数据。报告指出,咖啡因的半衰期是五到六小时,下午三点的咖啡到晚上九点仍有一半留在体内。",
            "est_duration_s": 13.3, "source_block_ids": ["b1"], "source_pages": [1],
        }]
    },
    "【分镜输入】": {
        "scenes": [{
            "id": "sc01",
            "visual_desc": "演讲者在屏幕前讲解报告要点,会议室环境,蓝橙色扁平插画,无文字。",
            "image_prompt": "报告讲解场景,演讲者与展示屏,蓝橙色扁平插画,无文字",
        }]
    },
}

_AUDIO_DURATION = 4.0


def _tone_wav(path: Path, duration_s: float = _AUDIO_DURATION, freq: float = 440.0) -> None:
    rate = 48000
    frames = bytearray()
    for i in range(int(rate * duration_s)):
        value = int(8000 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)


def _fake_make_audio(text: str, out_path: Path, cfg, privacy_mode, job_dir):
    """合成音调音频 + 覆盖全旁白的 raw_marks(marks 主路径,不经 whisper)。"""
    _tone_wav(out_path)
    chunk = 15
    total = max(1, math.ceil(len(text) / chunk))
    marks = [
        {"text": text[i * chunk:(i + 1) * chunk],
         "start_s": _AUDIO_DURATION * i / total,
         "end_s": _AUDIO_DURATION * (i + 1) / total}
        for i in range(total)
    ]
    return {"provider": "synthetic-tts", "voice": cfg["tts"]["voice"], "request_id": None,
            "placeholder": False, "raw_marks": marks}


def _fake_generate_image(prompt: str, out_path: Path, cfg, privacy_mode, job_dir):
    from PIL import Image, ImageDraw

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1280, 720), (20, 50, 100))
    ImageDraw.Draw(img).rectangle((40, 40, 1240, 680), outline=(240, 140, 20), width=6)
    img.save(out_path, format="PNG")
    return {"provider": "synthetic", "model": None, "request_id": None,
            "width": 1280, "height": 720, "placeholder": False}


def test_e2e_smoke_full_pipeline_final_mp4(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: FakeLLM(_FAKE_RESPONSES))
    monkeypatch.setattr("doc2video.pipeline.p5_assets.make_audio", _fake_make_audio)
    monkeypatch.setattr("doc2video.pipeline.p5_assets.generate_image", _fake_generate_image)
    doc = tmp_path / "demo.md"
    doc.write_text("# 咖啡因与睡眠\n\n咖啡因通过阻断腺苷受体让人保持清醒。\n", encoding="utf-8")

    code = main(["run", str(doc), "--privacy", "offline"])
    assert code == 0

    job = next(p for p in (tmp_path / "ws").iterdir() if p.is_dir() and (p / "state.json").exists())
    state = StateStore(job).load()
    for stage, st in state.stages.items():
        assert st.status in TERMINAL_OK, f"{stage} 未达终态: {st.status}"

    # 门禁与发布决定
    qc = json.loads((job / "qc_report.json").read_text(encoding="utf-8"))
    assert qc["publish_allowed"] is True
    assert qc["status"] in {"succeeded", "succeeded_with_warnings"}
    assert next(x for x in qc["checks"] if x["name"] == "响度")["result"] == "pass"
    assert next(x for x in qc["checks"] if x["name"] == "时长")["result"] == "pass"
    final = job / "final" / "output.mp4"
    assert final.exists()

    # 字幕主路径(AC-3):≥95% cues 来自 native marks
    subs = json.loads((job / "subtitles.json").read_text(encoding="utf-8"))
    native = [c for c in subs["cues"] if c["source"] == "native"]
    assert len(native) / max(1, len(subs["cues"])) >= 0.95

    # 成片媒体断言:视频/音频轨、时长 ±5%、编码规格
    from doc2video.pipeline.p7_render import probe_media

    timeline = json.loads((job / "render_timeline.json").read_text(encoding="utf-8"))
    info = probe_media(final)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (video["codec_name"], audio["codec_name"]) == ("h264", "aac")
    duration = float(info["format"]["duration"])
    assert abs(duration - timeline["total_s"]) / timeline["total_s"] <= 0.05

    # offline 零云调用(隐私审计)
    egress = job / "egress_manifest.json"
    if egress.exists():
        assert json.loads(egress.read_text(encoding="utf-8")).get("calls", []) == []
