"""P8 QC 与发布:硬门禁、全量媒体复核、占位/预览隔离、硬链接晋升。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from difflib import SequenceMatcher
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..contracts import (
    AssetsManifest,
    EgressManifest,
    EgressReport,
    GroundedSummary,
    QCCheck,
    QCReport,
    ReleaseManifest,
    RenderManifest,
    RenderTimeline,
    ScenePlan,
    Script,
    Subtitles,
)
from ..state import atomic_write_text, sha256_file, utcnow
from .stages import StageResult

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class QCError(RuntimeError):
    pass


def _run(argv: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except OSError as exc:
        raise QCError(f"无法启动媒体工具: {exc}") from exc


def _probe(path: Path) -> dict[str, Any]:
    out = _run([
        FFPROBE, "-v", "error", "-show_entries",
        "format=duration,format_name,start_time:stream=codec_type,codec_name,width,height,pix_fmt,profile,level,r_frame_rate,avg_frame_rate,sample_rate,channels",
        "-of", "json", str(path),
    ], timeout=60)
    if out.returncode != 0:
        raise QCError(f"ffprobe 失败: {(out.stderr or out.stdout)[-1200:]}")
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise QCError(f"ffprobe 输出不是 JSON: {path}") from exc
    if "duration" in data.get("format", {}):
        data["format"]["duration"] = float(data["format"]["duration"])
    return data


def _check(name: str, method: str, result: str, detail: str, threshold: str | None = None) -> QCCheck:
    return QCCheck(name=name, method=method, result=result, detail=detail, threshold=threshold)  # type: ignore[arg-type]


def _fraction(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value)
    if "/" in text:
        left, right = text.split("/", 1)
        try:
            denominator = float(right)
            return float(left) / denominator if denominator else None
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _mp4_faststart(path: Path) -> bool:
    """检查 moov 是否位于 mdat 之前;P7 使用 +faststart 时应为真。"""
    try:
        with path.open("rb") as stream:
            offset = 0
            size_total = path.stat().st_size
            while offset + 8 <= size_total:
                header = stream.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[:4], "big")
                kind = header[4:8]
                header_size = 8
                if size == 1:
                    extended = stream.read(8)
                    if len(extended) < 8:
                        return False
                    size = int.from_bytes(extended, "big")
                    header_size = 16
                if size == 0:
                    size = size_total - offset
                if size < header_size:
                    return False
                if kind == b"mdat":
                    return False
                if kind == b"moov":
                    return True
                stream.seek(size - header_size, os.SEEK_CUR)
                offset += size
    except OSError:
        return False
    return False


def _full_decode(path: Path) -> tuple[bool, str]:
    out = _run([FFMPEG, "-hide_banner", "-v", "error", "-i", str(path), "-map", "0", "-f", "null", "-"])
    if out.returncode == 0:
        return True, "全量 decode-to-null 通过"
    return False, (out.stderr or out.stdout)[-1200:]


def _check_spec(info: dict[str, Any], scene_plan: ScenePlan, cfg: dict[str, Any], path: Path) -> QCCheck:
    streams = info.get("streams", [])
    video = next((x for x in streams if x.get("codec_type") == "video"), None)
    audio = next((x for x in streams if x.get("codec_type") == "audio"), None)
    expected_aspects = {scene.aspect for scene in scene_plan.scenes}
    expected_size = {(1920, 1080)} if expected_aspects == {"16:9"} else {(1080, 1920)} if expected_aspects == {"9:16"} else set()
    expected_fps = float(cfg["compose"]["fps"])
    expected_level = round(float(cfg["compose"]["level"]) * 10)
    actual_fps = _fraction(video.get("avg_frame_rate")) if video else None
    format_name = str(info.get("format", {}).get("format_name", ""))
    checks = [
        bool(video and video.get("codec_name") == "h264"),
        bool(video and str(video.get("profile", "")).lower() == "high"),
        bool(video and video.get("pix_fmt") == "yuv420p"),
        bool(video and (video.get("width"), video.get("height")) in expected_size),
        bool(actual_fps is not None and abs(actual_fps - expected_fps) < 0.05),
        bool(video and int(video.get("level", -1)) == expected_level),
        bool(audio and audio.get("codec_name") == "aac"),
        bool(audio and int(audio.get("sample_rate", 0)) == int(cfg["compose"]["audio_khz"])),
        bool(audio and int(audio.get("channels", 0)) == int(cfg["compose"]["audio_channels"])),
        "mp4" in format_name and _mp4_faststart(path),
    ]
    detail = (
        f"video={video.get('codec_name') if video else '?'}/"
        f"{video.get('profile') if video else '?'}/"
        f"{video.get('width') if video else '?'}x{video.get('height') if video else '?'} "
        f"fps={actual_fps if actual_fps is not None else '?'} "
        f"level={video.get('level') if video else '?'} "
        f"audio={audio.get('codec_name') if audio else '?'}/"
        f"{audio.get('sample_rate') if audio else '?'}Hz/"
        f"{audio.get('channels') if audio else '?'}ch faststart={_mp4_faststart(path)}"
    )
    return _check("编码规格", "ffprobe + MP4 box", "pass" if all(checks) else "fail", detail,
                  "H.264 High/yuv420p/25fps/level 4.1/AAC 48kHz stereo/faststart")


def _check_duration(info: dict[str, Any], timeline: RenderTimeline) -> QCCheck:
    actual = float(info.get("format", {}).get("duration", 0.0))
    expected = float(timeline.total_s)
    error = abs(actual - expected) / expected if expected else float("inf")
    return _check("时长", "ffprobe vs render_timeline", "pass" if error <= 0.05 else "fail",
                  f"actual={actual:.3f}s expected={expected:.3f}s relative_error={error:.2%}", "±5%")


def _check_silence(path: Path, duration: float) -> QCCheck:
    out = _run([
        FFMPEG, "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-50dB:d=2",
        "-f", "null", "-",
    ])
    if out.returncode != 0:
        return _check("静音", "ffmpeg silencedetect", "fail", (out.stderr or out.stdout)[-1000:], "最长 ≤2s,总比 ≤10%")
    text = out.stderr or ""
    durations = [float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", text)]
    total = sum(durations)
    longest = max(durations, default=0.0)
    ratio = total / duration if duration > 0 else 1.0
    ok = longest <= 2.0 and ratio <= 0.10
    return _check("静音", "ffmpeg silencedetect", "pass" if ok else "fail",
                  f"silent_total={total:.3f}s ratio={ratio:.2%} longest={longest:.3f}s", "最长 ≤2s,总比 ≤10%")


def _check_black(path: Path, style_name: str) -> QCCheck:
    if style_name == "tech-dark":
        return _check("黑帧", "blackdetect(style calibration)", "warn", "tech-dark 深色风格跳过平均亮度硬阈值")
    out = _run([
        FFMPEG, "-hide_banner", "-i", str(path), "-vf", "blackdetect=d=1.0:pix_th=0.10",
        "-an", "-f", "null", "-",
    ])
    if out.returncode != 0:
        return _check("黑帧", "ffmpeg blackdetect", "fail", (out.stderr or out.stdout)[-1000:])
    durations = [float(x) for x in re.findall(r"black_duration:\s*([\d.]+)", out.stderr or "")]
    longest = max(durations, default=0.0)
    if longest > 2.0:
        result = "fail"
    elif longest > 1.0:
        result = "warn"
    else:
        result = "pass"
    return _check("黑帧", "ffmpeg blackdetect", result, f"segments={len(durations)} longest={longest:.3f}s", ">2s fail, 1–2s warn")


def _normalize_text(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _check_subtitles(subtitles: Subtitles, script: Script, timeline: RenderTimeline) -> QCCheck:
    timeline_by_id = {x.id: x for x in timeline.scenes}
    scene_by_id = {x.id: x for x in script.scenes}
    problems: list[str] = []
    coverages: list[float] = []
    for scene_id, scene in scene_by_id.items():
        cues = sorted((x for x in subtitles.cues if x.scene_id == scene_id), key=lambda x: x.start_s)
        ts = timeline_by_id.get(scene_id)
        if not cues:
            problems.append(f"{scene_id}:无 Cue")
            continue
        if ts is None:
            problems.append(f"{scene_id}:缺 timeline")
            continue
        lo = ts.scene_start_s + ts.lead_s - 0.02
        hi = ts.scene_start_s + ts.lead_s + ts.audio_duration_s + 0.02
        for cue in cues:
            if cue.start_s < lo or cue.end_s > hi or cue.end_s <= cue.start_s:
                problems.append(f"{cue.id}:越界/逆序")
        for left, right in pairwise(cues):
            if right.start_s < left.end_s - 0.001:
                problems.append(f"{left.id}/{right.id}:时间交叠")
        expected = _normalize_text(scene.narration)
        actual = _normalize_text("".join(x.text for x in cues))
        coverage = SequenceMatcher(None, expected, actual).ratio() if expected else 0.0
        coverages.append(coverage)
        if coverage < 0.80:
            problems.append(f"{scene_id}:原文覆盖率 {coverage:.1%}<80%")
    if not scene_by_id:
        problems.append("讲稿无场景")
    detail = f"scenes={len(scene_by_id)} cues={len(subtitles.cues)} coverage_min={min(coverages, default=0):.1%}"
    return _check("字幕覆盖", "Subtitles + Script + Timeline", "pass" if not problems else "fail",
                  detail + (" problems=" + ";".join(problems[:5]) if problems else ""), "每场景 Cue 存在、无越界/交叠、覆盖率 ≥80%")


def _all_blocks(parsed: Any) -> set[str]:
    return {block.block_id for section in parsed.sections for block in section.blocks}


def _check_facts(parsed: Any, summary: GroundedSummary, script: Script) -> QCCheck:
    blocks = _all_blocks(parsed)
    facts = {fact.fact_id: fact for fact in summary.facts}
    problems: list[str] = []
    script_chapters = {scene.chapter for scene in script.scenes}
    for scene in script.scenes:
        if not scene.source_block_ids:
            problems.append(f"{scene.id}:无 source_block_ids")
        problems.extend(f"{scene.id}:未知 block {x}" for x in scene.source_block_ids if x not in blocks)
        for claim in scene.claims:
            if claim.fact_id not in facts:
                problems.append(f"{scene.id}:未知 fact {claim.fact_id}")
    for fact in summary.facts:
        problems.extend(f"{fact.fact_id}:未知 source block {x}" for x in fact.source_block_ids if x not in blocks)
    for chapter in summary.chapter_plan:
        if chapter.chapter not in script_chapters:
            problems.append(f"缺少章节 {chapter.chapter}")
    result = "pass" if not problems else "fail"
    return _check("事实与章节覆盖", "GroundedSummary + Script + ParsedDocument", result,
                  f"facts={len(facts)} chapters={len(script_chapters)} blocks={len(blocks)}"
                  + (" problems=" + ";".join(problems[:8]) if problems else ""),
                  "claims.fact_id 全命中、source_block_ids 有效、章节覆盖 100%")


def _check_loudness(path: Path) -> QCCheck:
    out = _run([FFMPEG, "-hide_banner", "-i", str(path), "-af", "ebur128=framelog=verbose", "-f", "null", "-"])
    if out.returncode != 0:
        return _check("响度", "ffmpeg ebur128", "fail", (out.stderr or out.stdout)[-1000:], "I=-16±3 LUFS, true peak≤-1dBFS")
    text = out.stderr or ""
    integrated = re.findall(r"(?:^|\n)\s*I:\s*(-?[\d.]+)\s*LUFS", text)
    peaks = re.findall(r"(?:True peak|Peak):\s*(-?[\d.]+)\s*dBFS", text, re.IGNORECASE)
    if not integrated:
        return _check("响度", "ffmpeg ebur128", "fail", "无法解析 integrated loudness", "I=-16±3 LUFS, true peak≤-1dBFS")
    lufs = float(integrated[-1])
    peak = float(peaks[-1]) if peaks else None
    ok = -19.0 <= lufs <= -13.0 and (peak is None or peak <= -1.0)
    detail = f"I={lufs:.2f} LUFS true_peak={peak if peak is not None else '?'} dBFS"
    return _check("响度", "ffmpeg ebur128", "pass" if ok else "fail", detail, "I=-16±3 LUFS, true peak≤-1dBFS")


def _check_placeholders(assets: AssetsManifest, cfg: dict[str, Any]) -> tuple[QCCheck, bool]:
    total = len(assets.scenes)
    placeholders = [x for x in assets.scenes if x.image and x.image.placeholder]
    ratio = len(placeholders) / total if total else 1.0
    max_ratio = float(cfg["image"].get("max_placeholder_ratio", 0.10))
    max_consecutive = int(cfg["image"].get("max_consecutive_placeholder", 2))
    longest = current = 0
    for scene in assets.scenes:
        if scene.image and scene.image.placeholder:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    over = ratio > max_ratio or longest > max_consecutive
    if over or placeholders:
        result = "warn"
    else:
        result = "pass"
    detail = f"count={len(placeholders)}/{total} ratio={ratio:.1%} longest_consecutive={longest}"
    return _check("占位率", "AssetsManifest", result, detail,
                  f"ratio≤{max_ratio:.1%}, consecutive≤{max_consecutive}"), over


def _write_egress_report(job_dir: Path) -> tuple[str, str]:
    calls = []
    manifest_path = job_dir / "egress_manifest.json"
    if manifest_path.exists():
        calls = EgressManifest.model_validate_json(manifest_path.read_text(encoding="utf-8")).calls
    report = EgressReport(generated_at=utcnow(), calls=calls)
    atomic_write_text(job_dir / "egress_report.json", report.model_dump_json(indent=2) + "\n")
    return "egress_report.json", "application/json"


def _promote_hardlink(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        os.link(source, temp)
        os.replace(temp, target)
    except OSError:
        if temp.exists():
            temp.unlink()
        raise


def _write_report(job_dir: Path, checks: list[QCCheck], status: str) -> None:
    passed = sum(x.result == "pass" for x in checks)
    warnings = sum(x.result == "warn" for x in checks)
    failed = sum(x.result == "fail" for x in checks)
    summary = f"QC {status}: {passed} pass, {warnings} warn, {failed} fail"
    report = QCReport(status=status, checks=checks, summary=summary, generated_at=utcnow())  # type: ignore[arg-type]
    atomic_write_text(job_dir / "qc_report.json", report.model_dump_json(indent=2) + "\n")


def stage_p8(job_dir: Path, cfg: dict[str, Any], opts: Any, stage: str | None = None) -> StageResult:
    """执行 P8;硬失败通过 StageResult.error 交给 runner 标记 failed,但 QC 报告仍先落盘。"""
    checks: list[QCCheck] = []
    artifacts: list[tuple[str, str]] = [("qc_report.json", "application/json")]
    try:
        scene_plan = ScenePlan.model_validate_json((job_dir / "scene_plan.json").read_text(encoding="utf-8"))
        assets = AssetsManifest.model_validate_json((job_dir / "assets_manifest.json").read_text(encoding="utf-8"))
        timeline = RenderTimeline.model_validate_json((job_dir / "render_timeline.json").read_text(encoding="utf-8"))
        subtitles = Subtitles.model_validate_json((job_dir / "subtitles.json").read_text(encoding="utf-8"))
        script = Script.model_validate_json((job_dir / "script.json").read_text(encoding="utf-8"))
        parsed = json.loads((job_dir / "parsed.json").read_text(encoding="utf-8"))
        summary = GroundedSummary.model_validate_json((job_dir / "grounded_summary.json").read_text(encoding="utf-8"))
        from ..contracts import ParsedDocument

        parsed_doc = ParsedDocument.model_validate(parsed)
        render_manifest_path = job_dir / "render_manifest.json"
        render_manifest = RenderManifest.model_validate_json(render_manifest_path.read_text(encoding="utf-8")) if render_manifest_path.exists() else None
        candidate = job_dir / "render" / "final.mp4"
        if render_manifest:
            entry = next((x for x in render_manifest.entries if x.path.endswith("final.mp4")), None)
            if entry:
                candidate = job_dir / entry.path
        if not candidate.exists():
            checks.append(_check("可播放性", "ffprobe", "fail", f"候选成片不存在: {candidate}"))
        else:
            if render_manifest:
                entry = next((x for x in render_manifest.entries if x.path == str(candidate.relative_to(job_dir)).replace("\\", "/")), None)
                if entry and sha256_file(candidate) != entry.sha256:
                    checks.append(_check("产物完整性", "RenderManifest SHA-256", "fail", "P7 final.mp4 哈希与 manifest 不符"))
                else:
                    checks.append(_check("产物完整性", "RenderManifest SHA-256", "pass", "P7 final.mp4 哈希一致"))
            else:
                checks.append(_check("产物完整性", "文件存在性", "warn", "缺少 render_manifest.json,仅按文件检查"))
            try:
                info = _probe(candidate)
                checks.append(_check("可播放性", "ffprobe", "pass", "ffprobe 解析通过"))
                decoded, detail = _full_decode(candidate)
                checks.append(_check("全量 decode", "ffmpeg decode-to-null", "pass" if decoded else "fail", detail))
                checks.append(_check_spec(info, scene_plan, cfg, candidate))
                checks.append(_check_duration(info, timeline))
                checks.append(_check_silence(candidate, float(info.get("format", {}).get("duration", 0))))
                checks.append(_check_black(candidate, scene_plan.style.name))
                checks.append(_check_loudness(candidate))
            except QCError as exc:
                checks.append(_check("可播放性", "ffprobe", "fail", str(exc)))
        checks.append(_check_subtitles(subtitles, script, timeline))
        checks.append(_check_facts(parsed_doc, summary, script))
        placeholder_check, over_placeholder = _check_placeholders(assets, cfg)
        checks.append(placeholder_check)
        egress_name, egress_mime = _write_egress_report(job_dir)
        artifacts.append((egress_name, egress_mime))
        hard_fail = any(x.result == "fail" for x in checks)
        review = over_placeholder or bool(getattr(opts, "preview", False))
        if getattr(opts, "preview", False):
            checks.append(_check("预览隔离", "RunOptions.preview", "warn", "preview 模式永不可晋升 final"))
        if hard_fail:
            status = "failed"
        elif review:
            status = "needs_review"
        elif any(x.result == "warn" for x in checks):
            status = "succeeded_with_warnings"
        else:
            status = "succeeded"
        _write_report(job_dir, checks, status)
        if status in {"succeeded", "succeeded_with_warnings"}:
            source = candidate
            final_path = job_dir / "final" / "output.mp4"
            _promote_hardlink(source, final_path)
            release = ReleaseManifest(
                staging_sha256=sha256_file(source), final_path="final/output.mp4",
                linked_at=utcnow(), qc_status=status,
            )
            atomic_write_text(job_dir / "release_manifest.json", release.model_dump_json(indent=2) + "\n")
            artifacts.append(("release_manifest.json", "application/json"))
        if status == "failed":
            failed_names = ", ".join(x.name for x in checks if x.result == "fail")
            return StageResult(artifacts=artifacts, error=f"P8 硬门禁失败: {failed_names}")
        return StageResult(artifacts=artifacts, warnings=[x.detail for x in checks if x.result == "warn"], needs_review=status == "needs_review")
    except Exception as exc:  # noqa: BLE001
        _write_report(job_dir, checks + [_check("P8 输入/契约", "Pydantic", "fail", str(exc))], "failed")
        return StageResult(artifacts=artifacts, error=f"P8 执行失败: {exc}")
