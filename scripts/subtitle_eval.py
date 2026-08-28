"""S2.3 字幕真值集评测:对照人工标注真值,输出句起止误差 P50/P95/max 与覆盖率。

真值未就绪时不产出任何指标(不造假指标);结论口径见 docs/qc/subtitle_truth_set.md。

用法:
    uv run python -m scripts.subtitle_eval --job <job_dir> --truth <truth.json> [--out <report.json>]

truth.json 格式(全片时间轴,单位秒):
    {"target_ms": 300, "segments": [{"text": "...", "start_s": 1.2, "end_s": 3.4, "scene_id": "sc01"(可选)}]}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MIN_IOU = 0.3  # 时间交叠比低于此值视为未匹配


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def _iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    inter = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return inter / union if union > 0 else 0.0


def evaluate(job_dir: Path, truth_path: Path) -> dict:
    subtitles = json.loads((job_dir / "subtitles.json").read_text(encoding="utf-8"))
    cues = subtitles.get("cues", [])
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    target_s = float(truth.get("target_ms", 300)) / 1000
    segments = truth.get("segments", [])
    if not segments:
        return {"status": "no_truth", "note": "真值集为空,不产出指标(不造假指标)", "publish_note": "不承诺 <300ms"}

    start_errors: list[float] = []
    end_errors: list[float] = []
    matched = 0
    for seg in segments:
        best, best_iou = None, 0.0
        for cue in cues:
            score = _iou(seg["start_s"], seg["end_s"], cue["start_s"], cue["end_s"])
            if score > best_iou:
                best, best_iou = cue, score
        if best is not None and best_iou >= MIN_IOU:
            matched += 1
            start_errors.append(abs(best["start_s"] - seg["start_s"]))
            end_errors.append(abs(best["end_s"] - seg["end_s"]))

    coverage = matched / len(segments)
    metrics = {
        "segments_total": len(segments),
        "segments_matched": matched,
        "coverage": round(coverage, 4),
        "start_err_p50_s": round(_percentile(start_errors, 50), 4),
        "start_err_p95_s": round(_percentile(start_errors, 95), 4),
        "start_err_max_s": round(max(start_errors, default=0.0), 4),
        "end_err_p50_s": round(_percentile(end_errors, 50), 4),
        "end_err_p95_s": round(_percentile(end_errors, 95), 4),
        "end_err_max_s": round(max(end_errors, default=0.0), 4),
    }
    ok = coverage >= 0.9 and metrics["start_err_p95_s"] <= target_s and metrics["end_err_p95_s"] <= target_s
    return {
        "status": "measured",
        "target_ms": int(target_s * 1000),
        "metrics": metrics,
        "verdict": "达标:保留 <300ms 承诺" if ok else "不达标:删除 <300ms 硬承诺,降级为预览级口径",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="字幕真值集评测(无人工真值则不产出指标)")
    parser.add_argument("--job", required=True, help="Job 目录(需含 subtitles.json)")
    parser.add_argument("--truth", required=True, help="真值集 JSON(人工标注)")
    parser.add_argument("--out", default=None, help="报告输出路径(默认 <job>/qc/subtitle_eval.json)")
    args = parser.parse_args(argv)

    job_dir = Path(args.job)
    truth_path = Path(args.truth)
    if not (job_dir / "subtitles.json").exists():
        print(f"错误: {job_dir} 缺少 subtitles.json", file=sys.stderr)
        return 2
    if not truth_path.is_file():
        report = {"status": "no_truth", "note": f"真值文件不存在: {truth_path}",
                  "publish_note": "无真值不承诺 <300ms(预览级口径)"}
    else:
        report = evaluate(job_dir, truth_path)

    out = Path(args.out) if args.out else job_dir / "qc" / "subtitle_eval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "measured" else 3


if __name__ == "__main__":
    sys.exit(main())
