"""P9 剪映草稿导出兼容层。

使用 pyJianYingDraft 生成原生 draft_content.json/draft_meta_info.json。所有时间使用
秒输入给库、由库写成微秒;doc2video_manifest.json 额外保留 render_timeline 的
确定性对齐视图。P9 是可选交付通道,失败不回写 P8,也不阻断已发布 MP4。
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..contracts import AssetsManifest, DraftExportReport, RenderTimeline, ScenePlan, Subtitles
from ..state import atomic_write_text, sha256_file
from .stages import StageResult


class DraftExportError(RuntimeError):
    pass


DRAFT_FORMAT = "doc2video-jianying-compat"
DRAFT_VERSION = "1.0"


def _trange_seconds(draft_api: Any, start_s: float, duration_s: float) -> Any:
    """pyJianYingDraft 的 trange(float) 解释为微秒,所以这里显式换算秒→微秒。"""
    return draft_api.trange(round(start_s * 1_000_000), round(duration_s * 1_000_000))


def _relative_job_file(job_dir: Path, value: str, label: str) -> Path:
    """解析 Job 内相对路径,拒绝绝对路径与目录穿越。"""
    raw = Path(value)
    if raw.is_absolute():
        raise DraftExportError(f"{label} 不得为绝对路径: {value}")
    root = job_dir.resolve()
    path = (job_dir / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise DraftExportError(f"{label} 越出 Job 根目录: {value}") from exc
    if not path.is_file():
        raise DraftExportError(f"{label} 不存在或不是文件: {value}")
    return path


def _rewrite_native_paths(path: Path, old_root: Path, new_root: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    old = str(old_root)
    new = str(new_root)

    def rewrite(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(old):
            return new + value[len(old):]
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    atomic_write_text(path, json.dumps(rewrite(data), ensure_ascii=False, indent=4) + "\n")


def _copy_media(source: Path, media_dir: Path, name: str) -> Path:
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / name
    # 草稿包与 Job 在同一文件系统时使用硬链接,否则回退为独立副本。
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)
    return target


def _canvas(scene_plan: ScenePlan) -> tuple[int, int]:
    aspects = {scene.aspect for scene in scene_plan.scenes}
    if aspects == {"16:9"}:
        return 1920, 1080
    if aspects == {"9:16"}:
        return 1080, 1920
    raise DraftExportError(f"剪映草稿不支持混合画幅: {sorted(aspects)}")


def _scene_maps(scene_plan: ScenePlan, assets: AssetsManifest, timeline: RenderTimeline) -> tuple[dict[str, Any], dict[str, Any]]:
    scenes = {scene.id: scene for scene in scene_plan.scenes}
    asset_map = {asset.scene_id: asset for asset in assets.scenes}
    timeline_map = {item.id: item for item in timeline.scenes}
    if set(scenes) != set(asset_map) or set(scenes) != set(timeline_map):
        raise DraftExportError("scene_plan/assets_manifest/render_timeline 的场景集合不一致")
    return asset_map, timeline_map


def _build_native_draft(
    job_dir: Path,
    scene_plan: ScenePlan,
    assets: AssetsManifest,
    subtitles: Subtitles,
    timeline: RenderTimeline,
    draft_dir: Path,
) -> list[tuple[str, str]]:
    try:
        import pyJianYingDraft as draft
    except ImportError as exc:
        raise DraftExportError("未安装 pyJianYingDraft,无法生成原生剪映草稿") from exc

    width, height = _canvas(scene_plan)
    asset_map, timeline_map = _scene_maps(scene_plan, assets, timeline)
    draft_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_name = f".{job_dir.name}.building-{uuid.uuid4().hex[:8]}"
    folder = draft.DraftFolder(str(draft_dir.parent))
    script = folder.create_draft(temp_name, width, height, 25, maintrack_adsorb=True)
    building_dir = draft_dir.parent / temp_name
    generated: list[tuple[str, str]] = []
    try:
        video_track = script.append_track(draft.TrackSpec(draft.TrackType.video, "doc2video-video"))
        audio_track = script.append_track(draft.TrackSpec(draft.TrackType.audio, "doc2video-narration"))
        text_track = script.append_track(draft.TrackSpec(draft.TrackType.text, "doc2video-subtitle"))
        compat_scenes: list[dict[str, Any]] = []

        for scene in scene_plan.scenes:
            scene_asset = asset_map[scene.id]
            scene_timeline = timeline_map[scene.id]
            if scene_asset.image is None or scene_asset.audio is None:
                raise DraftExportError(f"{scene.id}:缺少图片或音频资产")
            image_source = _relative_job_file(job_dir, scene_asset.image.path, f"{scene.id} 图片")
            audio_source = _relative_job_file(job_dir, scene_asset.audio.path, f"{scene.id} 音频")
            image_suffix = image_source.suffix.lower() or ".png"
            audio_suffix = audio_source.suffix.lower() or ".wav"
            image_target = _copy_media(image_source, building_dir / "media", f"{scene.id}_image{image_suffix}")
            audio_target = _copy_media(audio_source, building_dir / "media", f"{scene.id}_audio{audio_suffix}")
            image_rel = image_target.relative_to(building_dir).as_posix()
            audio_rel = audio_target.relative_to(building_dir).as_posix()
            scene_start = float(scene_timeline.scene_start_s)
            scene_total = float(scene_timeline.scene_total_s)
            audio_start = scene_start + float(scene_timeline.lead_s)
            audio_duration = float(scene_timeline.audio_duration_s)
            image_material = draft.VideoMaterial(str(image_target), material_name=f"{scene.id}_image")
            audio_material = draft.AudioMaterial(str(audio_target), material_name=f"{scene.id}_narration")
            script.add_segment(
                draft.VideoSegment(image_material, _trange_seconds(draft, scene_start, scene_total)),
                track=video_track,
            )
            script.add_segment(
                draft.AudioSegment(audio_material, _trange_seconds(draft, audio_start, audio_duration)),
                track=audio_track,
            )
            compat_scenes.append({
                "scene_id": scene.id,
                "video": {
                    "path": image_rel,
                    "target_start_us": round(scene_start * 1_000_000),
                    "target_end_us": round((scene_start + scene_total) * 1_000_000),
                },
                "audio": {
                    "path": audio_rel,
                    "target_start_us": round(audio_start * 1_000_000),
                    "target_end_us": round((audio_start + audio_duration) * 1_000_000),
                },
                "source_block_ids": scene.source_block_ids,
            })

        subtitle_segments: list[dict[str, Any]] = []
        for cue in subtitles.cues:
            if cue.scene_id not in timeline_map:
                raise DraftExportError(f"{cue.id}:引用不存在的场景 {cue.scene_id}")
            scene_timeline = timeline_map[cue.scene_id]
            low = scene_timeline.scene_start_s + scene_timeline.lead_s - 0.02
            high = low + scene_timeline.audio_duration_s + 0.02
            if cue.start_s < low or cue.end_s > high:
                raise DraftExportError(f"{cue.id}:字幕时间超出场景音频范围")
            duration = float(cue.end_s - cue.start_s)
            text_style = draft.TextStyle(
                size=5.0, bold=True, color=(1.0, 1.0, 1.0), align=1,
                auto_wrapping=True, max_line_width=0.82,
            )
            clip_settings = draft.ClipSettings(transform_y=-0.78)
            script.add_segment(
                draft.TextSegment(cue.text, _trange_seconds(draft, float(cue.start_s), duration),
                                  style=text_style, clip_settings=clip_settings),
                track=text_track,
            )
            subtitle_segments.append({
                "id": cue.id, "scene_id": cue.scene_id, "text": cue.text,
                "start_us": round(cue.start_s * 1_000_000),
                "end_us": round(cue.end_s * 1_000_000), "source": cue.source,
            })

        script.save()
        compat = {
            "format": DRAFT_FORMAT,
            "version": DRAFT_VERSION,
            "job_id": job_dir.name,
            "canvas": {"width": width, "height": height, "fps": 25},
            "duration_us": round(timeline.total_s * 1_000_000),
            "timeline_sha256": sha256_file(job_dir / "render_timeline.json"),
            "backend": "pyJianYingDraft",
            "backend_note": "剪映 7+ 的自动导出控件可能不可用,请在剪映中打开草稿后人工导出。",
            "scenes": compat_scenes,
            "subtitles": subtitle_segments,
        }
        atomic_write_text(building_dir / "doc2video_manifest.json", json.dumps(compat, ensure_ascii=False, indent=2) + "\n")
        atomic_write_text(
            building_dir / "README.md",
            "# doc2video 剪映草稿\n\n"
            "本目录由 pyJianYingDraft 生成,请在 Windows 剪映中打开对应草稿。\n"
            "剪映 7+ 的自动导出控件可能不可用,生成草稿后可人工打开并导出。\n",
        )
        generated.extend([
            (f"drafts/{temp_name}/draft_content.json", "application/json"),
            (f"drafts/{temp_name}/draft_meta_info.json", "application/json"),
            (f"drafts/{temp_name}/doc2video_manifest.json", "application/json"),
            (f"drafts/{temp_name}/README.md", "text/markdown"),
        ])
        generated.extend(
            (f"drafts/{temp_name}/media/{p.name}", "image/*" if "_image" in p.name else "audio/*")
            for p in (building_dir / "media").iterdir()
        )
        if draft_dir.exists():
            shutil.rmtree(draft_dir)
        os.replace(building_dir, draft_dir)
        _rewrite_native_paths(draft_dir / "draft_content.json", building_dir, draft_dir)
        generated = [(path.replace(f"drafts/{temp_name}/", f"drafts/{job_dir.name}/"), mime) for path, mime in generated]
        return generated
    except Exception:
        if building_dir.exists():
            shutil.rmtree(building_dir, ignore_errors=True)
        raise


def _write_report(job_dir: Path, report: DraftExportReport) -> None:
    atomic_write_text(job_dir / "draft_export_report.json", report.model_dump_json(indent=2) + "\n")


def stage_p9(job_dir: Path, cfg: dict[str, Any], opts: Any, stage: str | None = None) -> StageResult:
    """P9 可选导出;错误变成 warning,不改变 P8 qc/release,不阻断主链。"""
    report_path = "draft_export_report.json"
    if not bool(getattr(opts, "export_draft", False)):
        report = DraftExportReport(
            ok=False, draft_path=None, error=None,
            note="未启用 --export-draft,主链 MP4 不受影响。",
        )
        _write_report(job_dir, report)
        return StageResult(artifacts=[(report_path, "application/json")])

    try:
        scene_plan = ScenePlan.model_validate_json((job_dir / "scene_plan.json").read_text(encoding="utf-8"))
        assets = AssetsManifest.model_validate_json((job_dir / "assets_manifest.json").read_text(encoding="utf-8"))
        subtitles = Subtitles.model_validate_json((job_dir / "subtitles.json").read_text(encoding="utf-8"))
        timeline = RenderTimeline.model_validate_json((job_dir / "render_timeline.json").read_text(encoding="utf-8"))
        draft_dir = job_dir / "drafts" / job_dir.name
        artifacts = _build_native_draft(job_dir, scene_plan, assets, subtitles, timeline, draft_dir)
        report = DraftExportReport(
            ok=True, draft_path=f"drafts/{job_dir.name}", error=None,
            note="已生成 pyJianYingDraft 原生草稿;剪映 7+ 可能需要人工打开后导出。",
        )
        _write_report(job_dir, report)
        artifacts.append((report_path, "application/json"))
        return StageResult(artifacts=artifacts)
    except Exception as exc:  # noqa: BLE001
        report = DraftExportReport(
            ok=False, draft_path=None, error=str(exc),
            note="草稿导出失败,不影响 P8 已发布 MP4,请检查报告后重试。",
        )
        _write_report(job_dir, report)
        return StageResult(
            artifacts=[(report_path, "application/json")],
            warnings=[f"P9 草稿导出失败: {exc}"],
        )
