"""S3.4 性能基准:冷/热 × 短文/5000 字 × 横/竖屏,记录 P50/P95 入库。

诚实口径(必须随结果一起入库):
- 基准在离线替身链路下测量(假 LLM/合成 TTS/合成图片,与 tests/test_e2e_smoke.py 同一套替身),
  度量的是管线本身开销(解析/状态机/ffmpeg 渲染/QC),**不含** LLM/TTS/图片生成的真实网络延迟。
- 真实 Provider 的 P50/P95 需在发布候选环境实测后回填,不得用本脚本数字替代。
- 用法: uv run python scripts/perf_bench.py [--runs 3] [--out docs/spikes/perf_bench.json]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

# 独立执行时保证仓库根在 sys.path(依赖 tests.fake_llm 替身)。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doc2video import cli

# ── 替身(与 tests/test_e2e_smoke.py 同源) ──────────────────────

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
            "narration": "大家好,今天我们用五分钟解读这份报告的核心结论,先看它的整体框架与关键数据。"
                         "报告指出,咖啡因的半衰期是五到六小时,下午三点的咖啡到晚上九点仍有一半留在体内。",
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


# ── 基准执行 ────────────────────────────────────────────────


def _make_doc(path: Path, long: bool) -> None:
    body = "咖啡因通过阻断腺苷受体让人保持清醒,这一机制在摄入后三十分钟内开始生效。\n"
    if long:
        body = body * 260  # ≈ 5000 字输入(管线开销以解析/状态机为主)
    path.write_text("# 咖啡因与睡眠\n\n" + body, encoding="utf-8")


def _run_once(ws: Path, doc: Path, aspect: str) -> float:
    os.environ["DOC2VIDEO_WORKSPACE"] = str(ws)  # workspace_dir 原生支持环境变量覆盖
    t0 = time.perf_counter()
    code = cli.main(["run", str(doc), "--privacy", "offline", "--aspect", aspect])
    dt = time.perf_counter() - t0
    if code != 0:
        raise RuntimeError(f"基准作业失败,退出码 {code}")
    return dt


def main() -> int:
    parser = argparse.ArgumentParser(description="S3.4 性能基准(离线替身链路)")
    parser.add_argument("--runs", type=int, default=3, help="每组合运行次数")
    parser.add_argument("--out", default="docs/spikes/perf_bench.json")
    args = parser.parse_args()

    from doc2video import providers
    from doc2video.pipeline import p5_assets
    from tests.fake_llm import FakeLLM

    _orig_build_llm = providers.build_llm
    _orig_make_audio = p5_assets.make_audio
    _orig_generate_image = p5_assets.generate_image
    _orig_env = os.environ.get("DOC2VIDEO_WORKSPACE")
    providers.build_llm = lambda cfg: FakeLLM(_FAKE_RESPONSES)
    p5_assets.make_audio = _fake_make_audio
    p5_assets.generate_image = _fake_generate_image

    results: dict[str, dict] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="perf_bench_") as td:
            base = Path(td)
            short_doc, long_doc = base / "short.md", base / "long.md"
            _make_doc(short_doc, long=False)
            _make_doc(long_doc, long=True)
            matrix = [
                ("cold_short_16:9", short_doc, "16:9"),
                ("cold_short_9:16", short_doc, "9:16"),
                ("cold_long_16:9", long_doc, "16:9"),
                ("cold_long_9:16", long_doc, "9:16"),
                ("hot_short_16:9", short_doc, "16:9"),
                ("hot_short_9:16", short_doc, "9:16"),
                ("hot_long_16:9", long_doc, "16:9"),
                ("hot_long_9:16", long_doc, "9:16"),
            ]
            for name, doc, aspect in matrix:
                times = []
                for i in range(args.runs):
                    ws = base / f"ws_{name.replace(':', 'x')}_{i}"  # Windows 目录名禁用冒号
                    ws.mkdir(parents=True)
                    dt = _run_once(ws, doc, aspect)
                    times.append(round(dt, 3))
                    print(f"[{name}] run {i + 1}/{args.runs}: {dt:.1f}s")
                s = sorted(times)
                results[name] = {
                    "runs_s": times,
                    "p50_s": round(statistics.median(s), 3),
                    "p95_s": round(s[-1] if len(s) == 1 else s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)], 3),
                }
    finally:
        providers.build_llm = _orig_build_llm
        p5_assets.make_audio = _orig_make_audio
        p5_assets.generate_image = _orig_generate_image
        if _orig_env is None:
            os.environ.pop("DOC2VIDEO_WORKSPACE", None)
        else:
            os.environ["DOC2VIDEO_WORKSPACE"] = _orig_env

    report = {
        "schema": "perf_bench/v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "honesty_note": (
            "离线替身链路基准:度量管线本身开销(解析/状态机/ffmpeg 渲染/QC),"
            "不含 LLM/TTS/图片生成真实延迟;真实 Provider 数字须另行实测回填,不得用本数据替代。"
        ),
        "runs_per_combo": args.runs,
        "results": results,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n基准结果已写入 {out_path}")
    for name, r in results.items():
        print(f"  {name:20s} P50={r['p50_s']}s P95={r['p95_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
