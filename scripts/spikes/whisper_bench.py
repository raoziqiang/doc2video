"""M0 Spike:faster-whisper CUDA 路径 + 中文 RTF + aligner 选型依据。

结果写入 docs/spikes/whisper_bench.json。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "docs" / "spikes" / "_work"
RESULT = ROOT / "docs" / "spikes" / "whisper_bench.json"

TTS_TEXT = "大家好,今天我们聊聊咖啡因和睡眠的关系,很多人靠咖啡提神,却不知道它悄悄影响你的深度睡眠。"

results: list[dict] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"  {'✓' if ok else '✗'} {name:36s} {detail}")


def make_tts_clip() -> Path:
    import edge_tts

    out = WORK / "whisper_test.mp3"
    asyncio.run(
        edge_tts.Communicate(TTS_TEXT, "zh-CN-XiaoxiaoNeural").save(str(out))
    )
    return out


def char_coverage(recognized: str, original: str) -> float:
    orig = set(original.replace(",", "").replace(",", ""))
    rec = set(recognized)
    return len(orig & rec) / len(orig) if orig else 0.0


def main() -> int:
    print("faster-whisper Spike — CUDA 路径与中文基准")
    import ctranslate2

    cuda_types = ctranslate2.get_supported_compute_types("cuda")
    check("ctranslate2 CUDA compute types", bool(cuda_types), f"{cuda_types}")

    clip = make_tts_clip()
    print(f"  TTS 测试音频: {clip} ({clip.stat().st_size//1024}KB, {TTS_TEXT.__len__()} 字)")

    from faster_whisper import WhisperModel

    def try_transcribe(device: str, compute_type: str):
        model = WhisperModel("small", device=device, compute_type=compute_type)
        t0 = time.perf_counter()
        segments, info = model.transcribe(
            str(clip), language="zh", word_timestamps=True, vad_filter=True
        )
        segs = list(segments)
        return segs, info, time.perf_counter() - t0

    cuda_compute = "int8_float16" if "int8_float16" in cuda_types else (cuda_types[0] if cuda_types else "int8")
    try:
        segs, info, elapsed = try_transcribe("cuda", cuda_compute)
        device, compute_type = "cuda", cuda_compute
    except RuntimeError as exc:
        # 本机缺 CUDA 12 运行库(cublas64_12.dll)→ 声明 CPU 路径(方案允许)
        print(f"  CUDA 不可用({exc}),降级 CPU int8")
        segs, info, elapsed = try_transcribe("cpu", "int8")
        device, compute_type = "cpu", "int8"
    check("模型加载 small", True, f"device={device} compute_type={compute_type}")
    audio_dur = info.duration
    rtf = elapsed / audio_dur if audio_dur else float("inf")
    recognized = "".join(s.text for s in segs)
    cov = char_coverage(recognized, TTS_TEXT)
    words = [w for s in segs for w in (s.words or [])]
    check("中文识别 RTF", rtf < 1.0, f"rtf={rtf:.2f}x(音频 {audio_dur:.1f}s, 耗时 {elapsed:.1f}s)")
    check("识别文本覆盖", cov > 0.8, f"覆盖率={cov:.2f}(识别: {recognized[:40]}…)")
    check("词级时间戳", len(words) > 10, f"words={len(words)}(faster-whisper word_timestamps 可用)")

    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "cuda_compute_types": list(cuda_types),
        "cuda_usable": device == "cuda",
        "checks": results,
        "frozen": {
            "asr": f"faster-whisper small @ {device} {compute_type}(中文 RTF={rtf:.2f}x 实测)",
            "subtitle_primary": "edge-tts 原生 WordBoundary/SentenceBoundary marks",
            "aligner": "faster-whisper word_timestamps + 字符比例映射(确定性、零新依赖);"
                       "whisperX 不引入(torch 重依赖);stable-ts 列为 M3 可选增强",
            "asr_fallback_note": "ASR 回读仅做文本覆盖 QC 与低置信兜底,不单独支撑 <300ms 硬承诺",
        },
    }
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failed = [c for c in results if not c["ok"]]
    print(f"\n结论: {len(results)-len(failed)}/{len(results)} 通过 → {RESULT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
