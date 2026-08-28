"""P5 素材资产:原文图片/表格抽取、生成式图片、离线音频占位、缓存与时间轴。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import uuid
import wave
from pathlib import Path
from typing import Any

from ..cache import CacheStore, content_key
from ..contracts import (
    AssetsManifest,
    AudioAsset,
    EgressCall,
    EgressManifest,
    ImageAsset,
    NativeMark,
    ParsedDocument,
    RenderTimeline,
    SceneAssets,
    ScenePlan,
    TimelineScene,
)
from ..state import atomic_write_text, utcnow
from .stages import StageResult


class AssetError(RuntimeError):
    """素材不可用且无法安全降级。"""


def _load_egress(job_dir: Path) -> EgressManifest:
    path = job_dir / "egress_manifest.json"
    if path.exists():
        return EgressManifest.model_validate_json(path.read_text(encoding="utf-8"))
    return EgressManifest()


def _assert_egress_quota(job_dir: Path, cfg: dict[str, Any]) -> EgressManifest:
    """云调用前校验每作业调用配额;超限 → fail closed(不静默放行)。"""
    manifest = _load_egress(job_dir)
    limit = int(cfg["limits"].get("max_cloud_calls_per_job") or 0)
    if limit and len(manifest.calls) >= limit:
        raise AssetError(f"云调用超过配额 max_cloud_calls_per_job={limit},拒绝继续外发")
    return manifest


def _record_egress(
    job_dir: Path,
    manifest: EgressManifest,
    provider: str,
    fields_sent: list[str],
    client_request_uuid: str,
    request_id: str | None = None,
) -> None:
    """云调用成功后追加审计记录(原子写);P8 汇总进 egress_report。"""
    manifest.calls.append(EgressCall(
        provider=provider,
        fields_sent=fields_sent,
        client_request_uuid=client_request_uuid,
        request_id=request_id,
        at=utcnow(),
    ))
    atomic_write_text(job_dir / "egress_manifest.json", manifest.model_dump_json(indent=2) + "\n")


# ── 成本配额(方案 S1.2b:冻结单价事前预算;真实计费接入后切换) ─────
#: 冻结预估单价(美元)。image 为每次生成;edge-tts 按每千字折算。
UNIT_PRICE_USD = {"fal": 0.025, "edge-tts": 0.005}


def estimate_cost(provider: str, payload_chars: int = 0) -> float:
    if provider == "edge-tts":
        return UNIT_PRICE_USD["edge-tts"] * max(1, payload_chars) / 1000
    return UNIT_PRICE_USD.get(provider, 0.0)


def _job_cost(manifest: EgressManifest) -> float:
    """按次累计估算成本;edge-tts 无字数记录时以代表性负载 100 字折算。"""
    return sum(estimate_cost(c.provider, 100) for c in manifest.calls)


def _daily_cost(job_dir: Path) -> float:
    """汇总同 workspace 全部作业当日的估算成本(共享账本)。"""
    ledger = _cache_root(job_dir).parent / "cost_ledger.jsonl"
    today = utcnow().date().isoformat()
    total = 0.0
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("at", "")[:10] == today:
                total += float(rec.get("cost", 0.0))
    return total


def _append_cost_ledger(job_dir: Path, provider: str, cost: float) -> None:
    if cost <= 0:
        return
    ledger = _cache_root(job_dir).parent / "cost_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"job": job_dir.name, "provider": provider, "cost": cost,
                            "at": utcnow()}, ensure_ascii=False) + "\n")


def _assert_cost_budget(job_dir: Path, cfg: dict[str, Any], manifest: EgressManifest, this_call: float) -> None:
    """事前成本预算检查:单作业/单日估算超限 → fail closed。"""
    limits = cfg.get("limits", {})
    per_job = float(limits.get("max_cost_per_job") or 0)
    per_day = float(limits.get("max_cost_per_day") or 0)
    job_total = _job_cost(manifest) + this_call
    if per_job and job_total > per_job:
        raise AssetError(f"预估成本 ${job_total:.3f} 超过单作业上限 ${per_job:.2f},拒绝继续外发")
    if per_day and _daily_cost(job_dir) + this_call > per_day:
        raise AssetError(f"当日预估成本超过单日上限 ${per_day:.2f},拒绝继续外发")


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


def _intent_path(job_dir: Path) -> Path:
    return job_dir / "pending_requests.json"


def _load_intents(job_dir: Path) -> dict[str, Any]:
    path = _intent_path(job_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_intent(job_dir: Path, key: str, record: dict[str, Any]) -> None:
    intents = _load_intents(job_dir)
    intents[key] = record
    atomic_write_text(_intent_path(job_dir), json.dumps(intents, ensure_ascii=False, indent=2) + "\n")


def generate_image(prompt: str, out_path: Path, cfg: dict[str, Any], privacy_mode: str, job_dir: Path) -> dict[str, Any]:
    """FAL 直连适配器。无 FAL_KEY 或 offline 时明确产出占位,禁止伪造成功;云调用记入外发审计。

    提交安全(方案 S1.2a):提交前原子写 pending intent(prompt hash + client_request_uuid),
    响应后补记 request_id;恢复时对已有 request_id 只查询不重提。
    """
    if privacy_mode == "offline":
        _write_placeholder_png(out_path, "OFFLINE IMAGE PLACEHOLDER")
        return {"provider": "placeholder", "model": None, "request_id": None,
                "width": 1280, "height": 720, "placeholder": True}
    key = os.environ.get("FAL_KEY")
    if not key:
        _write_placeholder_png(out_path, "FAL_KEY REQUIRED")
        return {"provider": "placeholder", "model": None, "request_id": None,
                "width": 1280, "height": 720, "placeholder": True}
    egress = _assert_egress_quota(job_dir, cfg)
    this_cost = estimate_cost("fal")
    _assert_cost_budget(job_dir, cfg, egress, this_cost)
    # 直连队列 API:提交/轮询/下载,只在显式允许云调用时执行。
    import httpx

    endpoint = cfg["image"]["endpoint"]
    intent_key = "image:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    prior = _load_intents(job_dir).get(intent_key)
    if prior and prior.get("request_id") and not prior.get("done"):
        # 崩溃恢复:提交已被远端接受 → 只查询旧请求,绝不重新提交(避免重复计费)。
        request_id = prior["request_id"]
        status_url = f"https://queue.fal.run/{endpoint}/requests/{request_id}/status"
        result_url = f"https://queue.fal.run/{endpoint}/requests/{request_id}"
        status = httpx.get(status_url, headers={"Authorization": f"Key {key}"}, timeout=30).json()
        if status.get("status") != "COMPLETED":
            raise AssetError(f"FAL 已提交请求 {request_id} 状态={status.get('status')},等待后续 resume")
        result = httpx.get(result_url, headers={"Authorization": f"Key {key}"}, timeout=60).json()
        image_url = (result.get("images") or [{}])[0].get("url")
        if not image_url:
            raise AssetError(f"FAL 已提交请求 {request_id} 无可用结果")
        content = httpx.get(image_url, timeout=60).content
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(content)
        _save_intent(job_dir, intent_key, {**prior, "done": True})
        return {"provider": "fal", "model": cfg["image"]["model_id"], "request_id": request_id,
                "width": None, "height": None, "placeholder": False}

    client_uuid = uuid.uuid4().hex
    # 提交前预持久化:崩溃发生在提交→响应之间时,恢复只能依赖本记录核对。
    _save_intent(job_dir, intent_key, {"prompt_hash": intent_key.split(":", 1)[1],
                                       "client_request_uuid": client_uuid,
                                       "request_id": None, "done": False, "at": utcnow()})
    resp = httpx.post(
        f"https://queue.fal.run/{endpoint}",
        headers={"Authorization": f"Key {key}", "x-fal-client-request-uuid": client_uuid},
        json={"prompt": prompt, "image_size": "landscape_16_9"}, timeout=60,
    )
    if resp.status_code not in (200, 201, 202):
        # S3.3:错误体可能回显 prompt → 脱敏,只保留状态码。
        raise AssetError(f"FAL submit HTTP {resp.status_code} (响应体已脱敏,长度={len(resp.text)})")
    data = resp.json()
    request_id = data.get("request_id") or data.get("id")
    if not request_id:
        # 同步响应兼容
        image_url = (data.get("images") or [{}])[0].get("url")
        if not image_url:
            raise AssetError("FAL 响应缺少 request_id/images.url")
    else:
        _save_intent(job_dir, intent_key, {"prompt_hash": intent_key.split(":", 1)[1],
                                           "client_request_uuid": client_uuid,
                                           "request_id": request_id, "done": False, "at": utcnow()})
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
    _save_intent(job_dir, intent_key, {"prompt_hash": intent_key.split(":", 1)[1],
                                       "client_request_uuid": client_uuid,
                                       "request_id": request_id, "done": True, "at": utcnow()})
    _record_egress(job_dir, egress, "fal", ["prompt"], client_uuid, request_id)
    _append_cost_ledger(job_dir, "fal", this_cost)
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


def make_audio(text: str, out_path: Path, cfg: dict[str, Any], privacy_mode: str, job_dir: Path) -> dict[str, Any]:
    """edge-tts 仅在显式云模式调用;offline 使用可审计静音占位;云调用记入外发审计。

    marks 生产(方案 S1.1b):以 stream() 同时落盘音频与 WordBoundary 事件,
    返回音频内相对秒数的原始 marks(未经 normalize)。
    edge-tts 无幂等/查询接口:崩溃重发属 at-least-once,但每次调用必须入审计。
    """
    if privacy_mode == "offline":
        duration = max(1.0, len(text) / cfg["video"]["chars_per_second"])
        _write_silence(out_path, duration)
        return {"provider": "offline-silence-placeholder", "voice": None, "request_id": None,
                "placeholder": True}
    egress = _assert_egress_quota(job_dir, cfg)
    this_cost = estimate_cost("edge-tts", len(text))
    _assert_cost_budget(job_dir, cfg, egress, this_cost)
    client_uuid = uuid.uuid4().hex
    try:
        import edge_tts

        async def _save_with_marks() -> list[dict[str, Any]]:
            marks: list[dict[str, Any]] = []
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("wb") as f:
                async for chunk in edge_tts.Communicate(text, cfg["tts"]["voice"]).stream():
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        f.write(chunk["data"])
                    elif chunk.get("type") == "WordBoundary":
                        marks.append({
                            "text": chunk.get("text", ""),
                            # edge-tts 的 offset/duration 单位为 100ns,折算为秒。
                            "start_s": int(chunk.get("offset", 0)) / 1e7,
                            "end_s": (int(chunk.get("offset", 0)) + int(chunk.get("duration", 0))) / 1e7,
                        })
            return marks

        raw_marks = asyncio.run(_save_with_marks())
        _record_egress(job_dir, egress, "edge-tts", ["text", "voice"], client_uuid, None)
        _append_cost_ledger(job_dir, "edge-tts", this_cost)
        return {"provider": "edge-tts", "voice": cfg["tts"]["voice"], "request_id": None,
                "placeholder": False, "raw_marks": raw_marks}
    except Exception as exc:
        # S3.3:TTS 异常信息可能携带外发文本 → 只保留异常类型,不携带内容。
        raise AssetError(f"TTS 失败: {type(exc).__name__} (详情已脱敏)") from exc


def normalize_marks(raw: list[dict[str, Any]] | None, duration_s: float) -> list[NativeMark] | None:
    """按 NativeMark 契约规范化:越界裁剪、重叠去重、空文本丢弃;空结果返回 None(合法降级)。"""
    if not raw:
        return None
    marks: list[NativeMark] = []
    cursor = 0.0
    for item in sorted(raw, key=lambda m: float(m.get("start_s", 0.0))):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, min(float(item["start_s"]), duration_s))
        end = max(0.0, min(float(item["end_s"]), duration_s))
        start = max(start, cursor)  # 重叠去重:不得早于上一条的结尾
        if end <= start or start >= duration_s:
            continue
        marks.append(NativeMark(text=text, start_s=round(start, 3), end_s=round(end, 3)))
        cursor = end
    return marks or None


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
    # L-03:顶层显式 import 替代 __import__ 反模式。
    parsed = ParsedDocument.model_validate_json(
        (job_dir / "parsed.json").read_text(encoding="utf-8")
    )
    privacy_mode = getattr(opts, "privacy_mode", "offline")
    cache = CacheStore(_cache_root(job_dir))
    assets_dir = job_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    blocks = {b.block_id: b for s in parsed.sections for b in s.blocks}
    block_types = {b.block_id: b.type for s in parsed.sections for b in s.blocks}
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
            # 必须选取真正的表格块;第一个引用块可能是段落,直接取 [0] 会把段落渲染成表格图。
            table_bid = next(
                (bid for bid in scene.source_block_ids if block_types.get(bid) == "table"),
                None,
            )
            if table_bid is None:
                warnings.append(f"{scene.id}:rendered_table 引用中无表格块,回退首个引用块")
                table_bid = scene.source_block_ids[0]
            target = assets_dir / f"{scene.id}_table.png"
            rows = blocks[table_bid].text.split(" | ")
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
                meta = generate_image(prompt, target, cfg, privacy_mode, job_dir)
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
        # marks sidecar 与音频同生命周期:键由音频键派生(再哈希,保持 64 位十六进制格式)。
        marks_key = hashlib.sha256((audio_key + ":marks").encode("utf-8")).hexdigest()
        marks_path = assets_dir / f"{scene.id}_marks.json"
        audio_cached = cache.get(audio_key)
        if audio_cached:
            shutil.copy2(audio_cached, audio_path)
            audio_meta = {"provider": "cache", "voice": cfg["tts"]["voice"]}
            # 缓存命中同样恢复 marks(与音频同键的 sidecar);缺失 → P6 降级兜底。
            sidecar = cache.get(marks_key)
            if sidecar:
                shutil.copy2(sidecar, marks_path)
        else:
            audio_meta = make_audio(scene.narration, audio_path, cfg, privacy_mode, job_dir)
            raw_marks = audio_meta.pop("raw_marks", None)
            if raw_marks is not None:
                atomic_write_text(marks_path, json.dumps(raw_marks, ensure_ascii=False, indent=2) + "\n")
            if not audio_meta.get("placeholder"):
                cache.put(audio_key, audio_path)
                if marks_path.exists():
                    cache.put(marks_key, marks_path)
        if audio_meta.get("placeholder"):
            warnings.append(f"{scene.id}:音频为占位({audio_meta.get('provider')})")
        duration = _audio_duration(audio_path)
        native_marks = normalize_marks(
            json.loads(marks_path.read_text(encoding="utf-8")) if marks_path.exists() else None,
            duration,
        )
        audio = AudioAsset(path=str(audio_path.relative_to(job_dir)), duration_s=duration,
                           native_marks=native_marks,
                           provider=audio_meta["provider"], voice=audio_meta.get("voice"))
        scene_assets.append(SceneAssets(scene_id=scene.id, image=image, audio=audio))

    assets = AssetsManifest(scenes=scene_assets)
    timeline = compute_render_timeline(assets, cfg)
    # 成片总时长硬上限(--max-duration 优先,缺省取配置):超限即失败,不静默产出长片。
    duration_limit = float(getattr(opts, "max_duration", 0) or cfg["video"]["max_duration_s"])
    if timeline.total_s > duration_limit:
        raise AssetError(
            f"预计总时长 {timeline.total_s:.1f}s 超过上限 {duration_limit:.0f}s(可缩短讲稿或调低 max_duration)"
        )
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
