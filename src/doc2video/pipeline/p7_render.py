"""P7 ffmpeg 合成:场景片段、拼接、两遍 loudnorm 与 ASS 字幕烧录。"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..contracts import (
    ArtifactEntry,
    AssetsManifest,
    RenderManifest,
    RenderTimeline,
    ScenePlan,
    Subtitles,
)
from ..state import atomic_write_text, sha256_file, utcnow
from .stages import StageResult

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class RenderError(RuntimeError):
    pass


def _run(argv: list[str], cwd: Path | None = None, timeout: int = 300, retries: int = 2) -> subprocess.CompletedProcess:
    last: subprocess.CompletedProcess | None = None
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            out = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except OSError as exc:
            raise RenderError(f"无法启动 ffmpeg: {exc}") from exc
        last = out
        if out.returncode == 0:
            return out
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    stderr = (last.stderr if last else "")[-2000:]
    raise RenderError(f"ffmpeg 失败(rc={last.returncode if last else '?'}): {stderr}")


def probe_media(path: Path) -> dict[str, Any]:
    out = _run([
        FFPROBE, "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,pix_fmt,profile,level,sample_rate,channels",
        "-of", "json", str(path),
    ], retries=1)
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError(f"ffprobe 输出不是 JSON: {path}") from exc
    if "duration" in data.get("format", {}):
        data["format"]["duration"] = float(data["format"]["duration"])
    return data


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    total_cs = round(seconds * 100)
    hours, rem = divmod(total_cs, 360000)
    minutes, centis = divmod(rem, 6000)
    secs, cs = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", r"\N")


def build_ass(subtitles: Subtitles, font: str = "Microsoft YaHei") -> str:
    """生成 UTF-8 ASS;时间已是全片时间轴,不再二次偏移。字体由配置指定(L-04 可审计)。"""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},48,&H00FFFFFF,&H00000000,&H00000000,&H99000000,1,0,0,0,100,100,0,0,1,3,1,2,60,60,50,134",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Text",
    ]
    for cue in subtitles.cues:
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start_s)},{_ass_time(cue.end_s)},Default,{_ass_escape(cue.text)}"
        )
    return "\n".join(lines) + "\n"


def build_audio_mix_filter(narration_input: str, bgm_input: str, delay_ms: int, duration_s: float) -> str:
    """正确方向:旁白是 sidechain, BGM 是被压缩的 main。"""
    return (
        f"{narration_input}asplit=2[narration_sc][narration_raw];"
        f"[narration_raw]adelay={delay_ms}|{delay_ms},apad,atrim=duration={duration_s}[narration_delayed];"
        f"{bgm_input}adelay={delay_ms}|{delay_ms},apad,atrim=duration={duration_s}[bgm_delayed];"
        "[bgm_delayed][narration_sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[bgm_duck];"
        f"[narration_delayed][bgm_duck]amix=inputs=2:duration=first:normalize=0,atrim=duration={duration_s}[aout]"
    )


def _canvas(aspect: str) -> tuple[int, int]:
    return (1080, 1920) if aspect == "9:16" else (1920, 1080)


def _render_scene(scene: Any, asset: Any, timeline: Any, job_dir: Path, render_dir: Path, cfg: dict[str, Any]) -> tuple[Path, list[str]]:
    if asset.image is None or asset.audio is None:
        raise RenderError(f"{scene.id}:缺少图片或音频资产")
    image = job_dir / asset.image.path
    audio = job_dir / asset.audio.path
    if not image.exists() or not audio.exists():
        raise RenderError(f"{scene.id}:媒体文件不存在 image={image} audio={audio}")
    width, height = _canvas(scene.aspect)
    fps = int(cfg["compose"]["fps"])
    total = float(timeline.scene_total_s)
    frames = max(1, math.ceil(total * fps))
    fade = min(float(cfg["compose"]["fade_s"]), total / 2)
    fade_out = max(0.0, total - fade)
    lead_ms = round(float(timeline.lead_s) * 1000)
    # 放大后裁剪给 zoompan 留出运动空间;crop 只能使用 w:h,不能使用 WxH。
    vf = (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan=z='min(zoom+0.0008,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={width}x{height}:fps={fps},format=yuv420p,settb=1/{fps},"
        f"fade=t=in:st=0:d={fade},fade=t=out:st={fade_out}:d={fade}"
    )
    out = render_dir / "scenes" / f"{scene.id}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        FFMPEG, "-y", "-loop", "1", "-i", str(image), "-i", str(audio),
        "-filter_complex",
        f"[0:v]{vf}[v];[1:a]adelay={lead_ms}|{lead_ms},apad,atrim=duration={total},asetpts=PTS-STARTPTS[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{total:.6f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(cfg["compose"]["crf"]),
        "-profile:v", cfg["compose"]["profile"], "-level:v", str(cfg["compose"]["level"]),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", cfg["compose"]["audio_bitrate"],
        "-ar", str(cfg["compose"]["audio_khz"]), "-ac", str(cfg["compose"]["audio_channels"]),
        "-movflags", "+faststart", str(out),
    ]
    _run(argv, timeout=300)
    return out, argv


def _concat(scene_paths: list[Path], render_dir: Path) -> tuple[Path, list[str]]:
    listing = render_dir / "scenes.txt"
    listing.write_text("\n".join(f"file '{p.as_posix().replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'" for p in scene_paths) + "\n", encoding="utf-8")
    out = render_dir / "concat.mp4"
    argv = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)]
    _run(argv, cwd=render_dir, timeout=300)
    return out, argv


def _measure_loudnorm(source: Path, cfg: dict[str, Any]) -> dict[str, str]:
    filt = cfg["compose"].get("loudnorm", {})
    spec = f"loudnorm=I={filt.get('I', -16)}:TP={filt.get('TP', -1.5)}:LRA={filt.get('LRA', 11)}:print_format=json"
    out = _run([FFMPEG, "-y", "-i", str(source), "-af", spec, "-f", "null", "-"], timeout=300)
    matches = re.findall(r"\{[^{}]*\}", out.stderr or "", re.DOTALL)
    if not matches:
        raise RenderError("loudnorm 第一遍没有返回 JSON 测量结果")
    try:
        measured = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        raise RenderError("loudnorm 测量 JSON 解析失败") from exc
    required = ("input_i", "input_tp", "input_lra", "input_thresh")
    if not all(k in measured for k in required):
        raise RenderError(f"loudnorm 测量缺少字段: {required}")
    return {k: str(measured[k]) for k in required}


def _apply_loudnorm(source: Path, target: Path, measured: dict[str, str], cfg: dict[str, Any]) -> list[str]:
    filt = cfg["compose"].get("loudnorm", {})
    spec = (
        f"loudnorm=I={filt.get('I', -16)}:TP={filt.get('TP', -1.5)}:LRA={filt.get('LRA', 11)}:"
        f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:linear=true"
    )
    argv = [
        FFMPEG, "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy", "-af", spec, "-c:a", "aac", "-b:a", cfg["compose"]["audio_bitrate"],
        "-ar", str(cfg["compose"]["audio_khz"]), "-ac", str(cfg["compose"]["audio_channels"]),
        "-movflags", "+faststart", str(target),
    ]
    _run(argv, timeout=300)
    return argv


def _mix_bgm(source: Path, bgm: Path, target: Path, total_s: float, cfg: dict[str, Any]) -> list[str]:
    """BGM 混音:旁白为 sidechain 压抵 BGM;BGM 循环至全片长度。"""
    argv = [
        FFMPEG, "-y", "-i", str(source), "-stream_loop", "-1", "-i", str(bgm),
        "-filter_complex", build_audio_mix_filter("[0:a]", "[1:a]", 0, total_s),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", cfg["compose"]["audio_bitrate"],
        "-ar", str(cfg["compose"]["audio_khz"]), "-ac", str(cfg["compose"]["audio_channels"]),
        "-movflags", "+faststart", str(target),
    ]
    _run(argv, timeout=300)
    return argv


def _burn_subtitles(source: Path, ass_name: str, target: Path, render_dir: Path, cfg: dict[str, Any]) -> list[str]:
    argv = [
        FFMPEG, "-y", "-i", str(source), "-vf", f"ass={ass_name}",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(cfg["compose"]["crf"]),
        "-profile:v", cfg["compose"]["profile"], "-level:v", str(cfg["compose"]["level"]),
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(target),
    ]
    _run(argv, cwd=render_dir, timeout=300)
    return argv


def stage_p7(job_dir: Path, cfg: dict[str, Any], opts: Any, stage: str | None = None) -> StageResult:
    scene_plan = ScenePlan.model_validate_json((job_dir / "scene_plan.json").read_text(encoding="utf-8"))
    assets = AssetsManifest.model_validate_json((job_dir / "assets_manifest.json").read_text(encoding="utf-8"))
    timeline = RenderTimeline.model_validate_json((job_dir / "render_timeline.json").read_text(encoding="utf-8"))
    subtitles = Subtitles.model_validate_json((job_dir / "subtitles.json").read_text(encoding="utf-8"))
    assets_by_id = {x.scene_id: x for x in assets.scenes}
    timeline_by_id = {x.id: x for x in timeline.scenes}
    render_dir = job_dir / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    scene_paths: list[Path] = []
    for scene in scene_plan.scenes:
        path, _scene_argv = _render_scene(scene, assets_by_id[scene.id], timeline_by_id[scene.id], job_dir, render_dir, cfg)
        scene_paths.append(path)
    concat, _concat_argv = _concat(scene_paths, render_dir)
    measured = _measure_loudnorm(concat, cfg)
    staging = render_dir / "staging.mp4"
    audio_argv = _apply_loudnorm(concat, staging, measured, cfg)
    audio_source = staging
    mix_argv: list[str] = []
    bgm = getattr(opts, "bgm", None)
    if bgm:
        bgm_path = Path(bgm)
        if not bgm_path.is_absolute() and not bgm_path.exists():
            # L-04:BGM 已在 run 时快照入 Job,Job 内相对路径按 job_dir 解析。
            bgm_path = job_dir / bgm_path
        if not bgm_path.exists():
            raise RenderError(f"BGM 文件不存在: {bgm}")
        mixed = render_dir / "mixed.mp4"
        mix_argv = _mix_bgm(staging, bgm_path, mixed, float(timeline.total_s), cfg)
        audio_argv = mix_argv
        audio_source = mixed
    font = str(cfg.get("subtitle", {}).get("font", "Microsoft YaHei"))
    ass = render_dir / "subtitles.ass"
    ass.write_text(build_ass(subtitles, font), encoding="utf-8-sig")
    final = render_dir / "final.mp4"
    if getattr(opts, "no_burn_subs", False):
        # 不烧录字幕:成片直接取混音/归一后画面;ASS 仍保留供软字幕使用。
        shutil.copyfile(audio_source, final)
        final_argv = audio_argv
    else:
        final_argv = _burn_subtitles(audio_source, ass.name, final, render_dir, cfg)
    info = probe_media(final)
    v = next((x for x in info.get("streams", []) if x.get("codec_type") == "video"), None)
    a = next((x for x in info.get("streams", []) if x.get("codec_type") == "audio"), None)
    expected_w, expected_h = _canvas(scene_plan.scenes[0].aspect)
    duration = float(info.get("format", {}).get("duration", 0))
    if not v or not a or v.get("codec_name") != "h264" or a.get("codec_name") != "aac":
        raise RenderError("最终成片媒体流校验失败:需要 H.264 + AAC")
    if (v.get("width"), v.get("height"), v.get("pix_fmt")) != (expected_w, expected_h, "yuv420p"):
        raise RenderError(f"最终画幅/像素格式不符: {v.get('width')}x{v.get('height')} {v.get('pix_fmt')}")
    if abs(duration - timeline.total_s) > 0.15:
        raise RenderError(f"最终时长偏差过大: actual={duration:.3f} expected={timeline.total_s:.3f}")
    entries = [
        ArtifactEntry(path="render/staging.mp4", sha256=sha256_file(staging), size=staging.stat().st_size, mime="video/mp4"),
        ArtifactEntry(path="render/final.mp4", sha256=sha256_file(final), size=final.stat().st_size, mime="video/mp4"),
    ]
    render_manifest = RenderManifest(
        staging_path="render/staging.mp4", entries=entries, command_argv=final_argv,
        bgm_mix_argv=mix_argv,
        fonts=[font],
        timeline_ref_sha256=sha256_file(job_dir / "render_timeline.json"), committed_at=utcnow(),
    )
    atomic_write_text(job_dir / "render_manifest.json", render_manifest.model_dump_json(indent=2) + "\n")
    return StageResult(artifacts=[
        ("render/staging.mp4", "video/mp4"),
        ("render/final.mp4", "video/mp4"),
        ("render_manifest.json", "application/json"),
    ])
