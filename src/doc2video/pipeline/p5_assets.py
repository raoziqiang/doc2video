"""P5 素材资产:原文图片/表格抽取、生成式图片、离线音频占位、缓存与时间轴。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

from ..cache import CacheStore, content_key
from ..contracts import (
    AssetsManifest,
    AudioAsset,
    ImageAsset,
    RenderTimeline,
    SceneAssets,
    ScenePlan,
    TimelineScene,
)
from ..state import atomic_write_text
from .stages import StageResult


class AssetError(RuntimeError):
    """素材不可用且无法安全降级。"""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_root(job_dir: Path) -> Path:
    # 同一 workspace 下的作业共享缓存,不同作业可复用相同请求结果。
    return job_dir.parent / ".cache" / "assets"


def _write_placeholder_png(path: Path, label: str = "PLACEHOLDER") -> None:
    """生成可被 ffmpeg 读取的确定性 PNG,不伪装为真实生成结果。"""
    from PIL import Image, ImageDraw, ImageFont

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1280, 720), (35, 45, 65))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 42)
    except OSError:
        font = ImageFont.load_default()
    draw.rectangle((30, 30, 1250, 690), outline=(232, 137, 12), width=5)
    draw.text((80, 320), label, fill=(245, 247, 250), font=font)
    img.save(path, format="PNG")


def render_table_image(rows: list[str], out: Path) -> Path:
    """把 P1 的稳定表格文本渲染为可复现 PNG。"""
    from PIL import Image, ImageDraw, ImageFont

    parsed = [[cell.strip() for cell in row.replace(" ; ", " | ").split("|")] for row in rows]
    parsed = [r for r in parsed if any(r)]
    if not parsed:
        raise AssetError("表格没有可渲染行")
    cols = max(len(r) for r in parsed)
    parsed = [r + [""] * (cols - len(r)) for r in parsed]
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
    except OSError:
        font = ImageFont.load_default()
    widths = [max(180, max(len(row[c]) for row in parsed) * 32 + 40) for c in range(cols)]
    row_h = 58
    img = Image.new("RGB", (sum(widths), row_h * len(parsed)), "white")
    draw = ImageDraw.Draw(img)
    x = 0
    for c, width in enumerate(widths):
        y = 0
        for r, row in enumerate(parsed):
            fill = (230, 238, 248) if r == 0 else (255, 255, 255)
            draw.rectangle((x, y, x + width, y + row_h), fill=fill, outline=(90, 105, 125), width=1)
            draw.text((x + 14, y + 13), row[c], fill=(20, 30, 45), font=font)
            y += row_h
        x += width
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG")
    return out


def generate_image(prompt: str, out_path: Path, cfg: dict[str, Any], privacy_mode: str) -> dict[str, Any]:
    """FAL 直连适配器。无 FAL_KEY 或 offline 时明确产出占位,禁止伪造成功。"""
    if privacy_mode == "offline":
        _write_placeholder_png(out_path, "OFFLINE IMAGE PLACEHOLDER")
        return {"provider": "placeholder", "model": None, "request_id": None,
                "width": 1280, "height": 720, "placeholder": True}
    key = os.environ.get("FAL_KEY")
    if not key:
        _write_placeholder_png(out_path, "FAL_KEY REQUIRED")
        return {"provider": "placeholder", "model": None, "request_id": None,
                "width": 1280, "height": 720, "placeholder": True}
    # 直连队列 API:提交/轮询/下载,只在显式允许云调用时执行。
    import httpx

    endpoint = cfg["image"]["endpoint"]
    resp = httpx.post(
        f"https://queue.fal.run/{endpoint}",
        headers={"Authorization": f"Key {key}"},
        json={"prompt": prompt, "image_size": "landscape_16_9"}, timeout=60,
    )
    if resp.status_code not in (200, 201, 202):
        raise AssetError(f"FAL submit HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    request_id = data.get("request_id") or data.get("id")
    if not request_id:
        # 同步响应兼容
        image_url = (data.get("images") or [{}])[0].get("url")
        if not image_url:
            raise AssetError("FAL 响应缺少 request_id/images.url")
    else:
        status_url = data.get("status_url", f"https://queue.fal.run/{endpoint}/requests/{request_id}/status")
        result_url = data.get("response_url", f"https://queue.fal.run/{endpoint}/requests/{request_id}")
        image_url = None
        for _ in range(120):
            status = httpx.get(status_url, headers={"Authorization": f"Key {key}"}, timeout=30).json()
            if status.get("status") == "COMPLETED":
                result = httpx.get(result_url, headers={"Authorization": f"Key {key}"}, timeout=60).json()
                image_url = (result.get("images") or [{}])[0].get("url")
                break
            if status.get("status") in ("FAILED", "CANCELLED"):
                raise AssetError(f"FAL request {request_id} 状态={status.get('status')}")
            import time
            time.sleep(2)
        if not image_url:
            raise AssetError(f"FAL request {request_id} 超时")
    content = httpx.get(image_url, timeout=60).content
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(content)
    return {"provider": "fal", "model": cfg["image"]["model_id"], "request_id": request_id,
            "width": None, "height": None, "placeholder": False}


def _write_silence(path: Path, duration_s: float) -> None:
    frames = max(1, int(16000 * duration_s))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\0\0" * frames)


def make_audio(text: str, out_path: Path, cfg: dict[str, Any], privacy_mode: str) -> dict[str, Any]:
    """edge-tts 仅在显式云模式调用;offline 使用可审计静音占位。"""
    if privacy_mode == "offline":
        duration = max(1.0, len(text) / cfg["video"]["chars_per_second"])
        _write_silence(out_path, duration)
        return {"provider": "offline-silence-placeholder", "voice": None, "request_id": None,
                "placeholder": True}
    try:
        import edge_tts

        async def _save() -> None:
            await edge_tts.Communicate(text, cfg["tts"]["voice"]).save(str(out_path))

        asyncio.run(_save())
        return {"provider": "edge-tts", "voice": cfg["tts"]["voice"], "request_id": None,
                "placeholder": False}
    except Exception as exc:
        raise AssetError(f"TTS 失败: {exc}") from exc


def _audio_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as f:
            return f.getnframes() / f.getframerate()
    except (wave.Error, EOFError):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode != 0:
            raise AssetError(f"无法探测音频时长: {path}")
        return float(probe.stdout.strip())


def compute_render_timeline(assets: AssetsManifest, cfg: dict[str, Any]) -> RenderTimeline:
    lead = float(cfg["compose"]["lead_s"])
    trail = float(cfg["compose"]["trail_s"])
    fade = float(cfg["compose"]["fade_s"])
    start = 0.0
    scenes: list[TimelineScene] = []
    for item in assets.scenes:
        if item.audio is None:
            raise AssetError(f"{item.scene_id} 缺少音频,不能建立时间轴")
        total = lead + item.audio.duration_s + trail
        scenes.append(TimelineScene(
            id=item.scene_id, scene_start_s=start, lead_s=lead,
            audio_duration_s=item.audio.duration_s, trail_s=trail,
            scene_total_s=total, fade_out_start_s=start + total - fade,
        ))
        start += total
    return RenderTimeline(scenes=scenes, total_s=start)


def stage_p5(job_dir: Path, cfg: dict[str, Any], opts: Any, stage: str | None = None) -> StageResult:
    scene_plan = ScenePlan.model_validate_json((job_dir / "scene_plan.json").read_text(encoding="utf-8"))
    parsed = __import__("doc2video.contracts", fromlist=["ParsedDocument"]).ParsedDocument.model_validate_json(
        (job_dir / "parsed.json").read_text(encoding="utf-8")
    )
    privacy_mode = getattr(opts, "privacy_mode", "offline")
    cache = CacheStore(_cache_root(job_dir))
    assets_dir = job_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    blocks = {b.block_id: b for s in parsed.sections for b in s.blocks}
    scene_assets: list[SceneAssets] = []
    warnings: list[str] = []
    placeholder_images = 0
    for scene in scene_plan.scenes:
        # 图片:请求 key 包含 prompt/style/model/画幅,防止不同配置碰撞。
        image: ImageAsset | None = None
        if scene.visual_source == "extracted_image":
            src = job_dir / (scene.extracted_ref or "")
            if not src.exists():
                warnings.append(f"{scene.id}: extracted image 不存在: {src}")
            else:
                target = assets_dir / f"{scene.id}{src.suffix.lower()}"
                shutil.copy2(src, target)
                image = ImageAsset(path=str(target.relative_to(job_dir)), cache_key=_sha256(target),
                                   provider="extracted", width=None, height=None)
        elif scene.visual_source == "rendered_table":
            target = assets_dir / f"{scene.id}_table.png"
            rows = blocks[scene.source_block_ids[0]].text.split(" | ")
            render_table_image(rows, target)
            image = ImageAsset(path=str(target.relative_to(job_dir)), cache_key=_sha256(target),
                               provider="rendered_table", width=None, height=None)
        else:
            prompt = scene.image_prompt or scene.visual_desc
            key = content_key(prompt, cfg["image"]["model_id"], scene.aspect)
            cached = cache.get(key)
            target = assets_dir / f"{scene.id}.png"
            if cached:
                shutil.copy2(cached, target)
                meta = {"provider": "cache", "model": cfg["image"]["model_id"], "placeholder": False}
            else:
                meta = generate_image(prompt, target, cfg, privacy_mode)
                if not meta.get("placeholder"):
                    cache.put(key, target)
            placeholder = bool(meta.get("placeholder"))
            placeholder_images += int(placeholder)
            if placeholder:
                warnings.append(f"{scene.id}:生成式图片为占位({meta.get('provider')})")
            image = ImageAsset(path=str(target.relative_to(job_dir)), cache_key=key,
                               provider=meta["provider"], model=meta.get("model"),
                               request_id=meta.get("request_id"), width=meta.get("width"),
                               height=meta.get("height"), placeholder=placeholder)

        audio_path = assets_dir / f"{scene.id}.wav"
        audio_key = content_key(scene.narration, cfg["tts"]["provider"], cfg["tts"]["voice"])
        audio_cached = cache.get(audio_key)
        if audio_cached:
            shutil.copy2(audio_cached, audio_path)
            audio_meta = {"provider": "cache", "voice": cfg["tts"]["voice"]}
        else:
            audio_meta = make_audio(scene.narration, audio_path, cfg, privacy_mode)
            if not audio_meta.get("placeholder"):
                cache.put(audio_key, audio_path)
        if audio_meta.get("placeholder"):
            warnings.append(f"{scene.id}:音频为占位({audio_meta.get('provider')})")
        duration = _audio_duration(audio_path)
        audio = AudioAsset(path=str(audio_path.relative_to(job_dir)), duration_s=duration,
                           provider=audio_meta["provider"], voice=audio_meta.get("voice"))
        scene_assets.append(SceneAssets(scene_id=scene.id, image=image, audio=audio))

    assets = AssetsManifest(scenes=scene_assets)
    timeline = compute_render_timeline(assets, cfg)
    atomic_write_text(job_dir / "assets_manifest.json", assets.model_dump_json(indent=2) + "\n")
    atomic_write_text(job_dir / "render_timeline.json", timeline.model_dump_json(indent=2) + "\n")
    max_ratio = float(cfg["image"]["max_placeholder_ratio"])
    ratio = placeholder_images / max(1, len(scene_plan.scenes))
    if ratio > max_ratio:
        warnings.append(f"占位图片比例 {ratio:.1%} 超过门槛 {max_ratio:.1%}")
    return StageResult(
        artifacts=[("assets_manifest.json", "application/json"), ("render_timeline.json", "application/json")],
        warnings=warnings,
        needs_review=bool(warnings),
    )
