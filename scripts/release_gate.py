"""发布级验收 gate。

默认执行本地可重复检查;``--media-smoke`` 额外真实运行 30 秒 P7→P8→P9 链路;
``--live`` 才触发 Ollama、edge-tts、faster-whisper、ffmpeg Spike 与 FAL 直连 smoke。
任何 fail/blocked 都 fail-closed,不生成 release-ready=true。报告不记录凭据值。

S3.1 绑定机制(不可手工绕过):
- ``run --candidate <产物>``:gate 结果与候选产物 SHA-256 绑定,落盘 ``docs/release/``;
- ``verify <产物>``:发布前唯一校验入口——必须存在 release_ready 且 digest 一致的 gate 报告,
  否则 fail-closed(退出码 2)。不提供任何 skip/force 旁路参数。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from doc2video import __version__
from doc2video.config import load_config
from doc2video.contracts import (
    AssetsManifest,
    AudioAsset,
    Block,
    ChapterPlanItem,
    ChapterSummary,
    Coverage,
    Cue,
    GroundedSummary,
    ImageAsset,
    ParsedDocument,
    ParsedMeta,
    RenderTimeline,
    SceneAssets,
    ScenePlan,
    ScenePlanScene,
    Script,
    ScriptScene,
    Section,
    Subtitles,
    TimelineScene,
)
from doc2video.pipeline.p7_render import stage_p7
from doc2video.pipeline.p8_qc import stage_p8
from doc2video.pipeline.p9_jianying import stage_p9
from doc2video.state import atomic_write_text, sha256_file

GateStatus = Literal["pass", "warn", "blocked", "fail"]


@dataclass(frozen=True)
class GateCheck:
    name: str
    status: GateStatus
    detail: str


def overall_status(checks: list[GateCheck]) -> str:
    if any(check.status == "fail" for check in checks):
        return "failed"
    if any(check.status == "blocked" for check in checks):
        return "blocked"
    if any(check.status == "warn" for check in checks):
        return "warnings"
    return "passed" if checks and all(check.status == "pass" for check in checks) else "failed"


def credential_state(value: str | None) -> str:
    return "configured" if value else "missing"


def live_scope_check(live: bool) -> GateCheck:
    if live:
        return GateCheck("发布范围:live smoke", "pass", "已请求 Provider 真实 smoke")
    return GateCheck("发布范围:live smoke", "blocked", "未传 --live;仅本地 gate,不代表发布就绪")


def redact(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    clean = text
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        clean = clean.replace(secret, "[REDACTED]")
    return clean


def candidate_descriptor(candidate: Path | None) -> dict[str, Any] | None:
    """S3.1:候选产物指纹(路径/大小/SHA-256),gate 报告与其绑定。"""
    if candidate is None:
        return None
    if not candidate.is_file():
        raise FileNotFoundError(f"候选产物不存在: {candidate}")
    return {
        "path": str(candidate),
        "size": candidate.stat().st_size,
        "sha256": sha256_file(candidate),
    }


def build_report(checks: list[GateCheck], project_version: str,
                 candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    status = overall_status(checks)
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "project_version": project_version,
        "status": status,
        "release_ready": status == "passed",
        "candidate": candidate,
        "checks": [
            {"name": check.name, "status": check.status, "detail": check.detail}
            for check in checks
        ],
    }


def verify_candidate(root: Path, candidate: Path) -> tuple[bool, str]:
    """S3.1 发布前唯一校验入口:必须存在 release_ready 且 digest 与候选产物一致的 gate 报告。

    fail-closed:无报告、报告未就绪、digest 不符均拒绝;不提供任何旁路参数。
    """
    if not candidate.is_file():
        return False, f"候选产物不存在: {candidate}"
    digest = sha256_file(candidate)
    release_dir = root / "docs" / "release"
    if not release_dir.is_dir():
        return False, "docs/release/ 不存在:从未运行过 gate"
    reports = sorted(release_dir.glob("*.json"))
    if not reports:
        return False, "docs/release/ 无 gate 报告:必须先运行 release_gate run"
    rejected: str | None = None
    for path in reports:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        bound = report.get("candidate")
        if not isinstance(bound, dict) or bound.get("sha256") != digest:
            continue
        if report.get("release_ready") is True:
            return True, f"候选产物与 {path.name} 绑定且 gate 全绿"
        rejected = f"gate 报告 {path.name} 与产物绑定但状态为 {report.get('status')},不可发布"
    if rejected:
        return False, rejected
    return False, f"无 gate 报告绑定候选产物(前 12 位 {digest[:12]});重跑带 --candidate 的 gate"


def _known_secrets(root: Path) -> list[str]:
    values = [os.environ.get("FAL_KEY", ""), os.environ.get("GOOGLE_API_KEY", "")]
    sensitive_markers = ("API_KEY", "ACCESS_KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "AUTH")
    values.extend(
        value for name, value in os.environ.items()
        if value and any(marker in name.upper() for marker in sensitive_markers)
    )
    from dotenv import dotenv_values

    try:
        dotenv_data = dotenv_values(root / ".env")
    except (OSError, UnicodeError, ValueError):
        dotenv_data = {}
    values.extend(str(value) for value in dotenv_data.values() if value)
    return list(dict.fromkeys(value for value in values if value))


def run_command(
    name: str,
    argv: list[str],
    root: Path,
    timeout_s: int = 600,
    secrets: list[str] | tuple[str, ...] = (),
) -> GateCheck:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            argv, cwd=root, capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return GateCheck(name, "fail", f"超时({timeout_s}s)")
    except OSError as exc:
        return GateCheck(name, "fail", f"无法启动: {exc}")
    elapsed = time.perf_counter() - started
    output = redact((result.stdout or "") + (result.stderr or ""), list(secrets))
    detail = output.strip()[-1200:] or f"returncode={result.returncode}"
    detail = f"{detail} (elapsed={elapsed:.1f}s)"
    return GateCheck(name, "pass" if result.returncode == 0 else "fail", detail)


def check_schemas(root: Path) -> GateCheck:
    try:
        from doc2video.contracts.generate_schemas import REGISTRY, SCHEMAS_DIR, generate_all

        with tempfile.TemporaryDirectory(prefix="doc2video-schema-") as temp:
            generated = generate_all(Path(temp))
            mismatches = []
            for name in REGISTRY:
                committed = (SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
                fresh = generated[name].read_text(encoding="utf-8")
                if committed != fresh:
                    mismatches.append(name)
        return GateCheck("Schema 无漂移", "pass" if not mismatches else "fail",
                         "全部一致" if not mismatches else f"不一致: {mismatches}")
    except Exception as exc:  # noqa: BLE001
        return GateCheck("Schema 无漂移", "fail", redact(f"无法验证 Schema: {exc}", _known_secrets(root)))


def check_runtime(root: Path) -> list[GateCheck]:
    checks: list[GateCheck] = []
    for executable in ("ffmpeg", "ffprobe"):
        path = shutil.which(executable)
        checks.append(GateCheck(f"runtime:{executable}", "pass" if path else "fail", path or "未找到"))
    try:
        import pyJianYingDraft  # noqa: F401

        checks.append(GateCheck("runtime:pyJianYingDraft", "pass", "已安装"))
    except ImportError:
        checks.append(GateCheck("runtime:pyJianYingDraft", "fail", "未安装"))
    font = Path(r"C:\Windows\Fonts\msyh.ttc")
    checks.append(GateCheck("runtime:CJK 字体", "pass" if font.exists() else "warn", str(font)))
    return checks


def _make_image(path: Path, index: int) -> None:
    from PIL import Image, ImageDraw

    colors = [(27, 58, 107), (45, 90, 130), (80, 50, 120)]
    image = Image.new("RGB", (640, 360), colors[index % len(colors)])
    draw = ImageDraw.Draw(image)
    draw.rectangle((35, 35, 605, 325), outline=(232, 137, 12), width=8)
    draw.ellipse((260, 100, 380, 220), fill=(245, 247, 250))
    image.save(path)


def _make_audio(path: Path, duration_s: float, frequency: int) -> None:
    rate = 48_000
    amplitude = 7_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        frames = bytearray()
        for index in range(round(rate * duration_s)):
            value = int(amplitude * math.sin(2 * math.pi * frequency * index / rate))
            frames.extend(struct.pack("<h", value))
        output.writeframes(frames)


def _build_media_smoke_job(root: Path) -> Path:
    job = root / "release-media-smoke"
    assets_dir = job / "assets"
    assets_dir.mkdir(parents=True)
    narration = (
        "这是发布级媒体链路的合成测试旁白,用于验证画面、音频、字幕、时间轴和质量门禁。"
        "它包含足够的中文字符,确保字幕覆盖率和实际音频时长检查都经过真实执行。"
    )
    scenes = []
    scene_assets = []
    timeline_scenes = []
    cues = []
    sections = []
    chapter_summaries = []
    chapter_plan = []
    for index in range(2):
        scene_id = f"sc{index + 1:02d}"
        chapter = f"第{index + 1}章"
        image_path = assets_dir / f"{scene_id}.png"
        audio_path = assets_dir / f"{scene_id}.wav"
        _make_image(image_path, index)
        _make_audio(audio_path, 14.0, 440 + index * 80)
        start = index * 15.0
        _make_scene = ScenePlanScene(
            id=scene_id, chapter=chapter, narration=narration, est_duration_s=33.0,
            visual_desc="蓝色演播室中的圆形信息图与演讲者,无文字。",
            visual_source="generated", image_prompt="扁平插画演播室信息图,无文字",
            aspect="16:9", source_block_ids=[f"b{index + 1}"], source_pages=[index + 1],
        )
        scenes.append(_make_scene)
        scene_assets.append(SceneAssets(
            scene_id=scene_id,
            image=ImageAsset(path=f"assets/{scene_id}.png", cache_key=("a" if index == 0 else "b") * 64,
                              provider="release-smoke", width=640, height=360),
            audio=AudioAsset(path=f"assets/{scene_id}.wav", duration_s=14.0, provider="release-smoke"),
        ))
        timeline_scenes.append(TimelineScene(
            id=scene_id, scene_start_s=start, lead_s=0.5, audio_duration_s=14.0,
            trail_s=0.5, scene_total_s=15.0, fade_out_start_s=start + 14.7,
        ))
        cues.append(Cue(
            id=f"{scene_id}-cu001", scene_id=scene_id, text=narration,
            start_s=start + 0.5, end_s=start + 14.5, source="aligned",
        ))
        block_id = f"b{index + 1}"
        sections.append(Section(
            id=f"s{index + 1}", level=1, heading=chapter,
            blocks=[Block(block_id=block_id, type="paragraph", text=narration, page=index + 1,
                          reading_order=index + 1)],
        ))
        chapter_summaries.append(ChapterSummary(
            section_ids=[f"s{index + 1}"], summary=narration[:20], source_block_ids=[block_id],
        ))
        chapter_plan.append(ChapterPlanItem(
            chapter=chapter, section_ids=[f"s{index + 1}"], planned_scenes=1,
        ))
    scene_plan = ScenePlan(
        style={"name": "flat-illustration", "prefix": "扁平插画风格,无文字", "negative": "text"},
        scenes=scenes,
    )
    script = Script(scenes=[ScriptScene(
        id=scene.id, chapter=scene.chapter, narration=scene.narration,
        est_duration_s=scene.est_duration_s, source_block_ids=scene.source_block_ids,
        source_pages=scene.source_pages, claims=[],
    ) for scene in scenes])
    parsed = ParsedDocument(
        meta=ParsedMeta(source="release-smoke.md", type="md", pages=2, chars=len(narration) * 2,
                        parser_version="release-smoke"),
        title="发布级媒体链路测试", sections=sections,
    )
    grounded = GroundedSummary(
        doc_summary="发布级媒体链路测试摘要", key_points=[], chapter_plan=chapter_plan,
        chapter_summaries=chapter_summaries, facts=[], coverage=Coverage(blocks_seen=2, blocks_total=2),
    )
    timeline = RenderTimeline(scenes=timeline_scenes, total_s=30.0)
    subtitles = Subtitles(cues=cues)
    for name, model in (
        ("scene_plan.json", scene_plan), ("script.json", script), ("parsed.json", parsed),
        ("grounded_summary.json", grounded), ("assets_manifest.json", AssetsManifest(scenes=scene_assets)),
        ("render_timeline.json", timeline), ("subtitles.json", subtitles),
    ):
        (job / name).write_text(model.model_dump_json(indent=2), encoding="utf-8")
    return job


def check_media_smoke(root: Path) -> GateCheck:
    try:
        with tempfile.TemporaryDirectory(prefix="doc2video-media-") as temp:
            job = _build_media_smoke_job(Path(temp))
            cfg = load_config()
            p7 = stage_p7(job, cfg, type("Opts", (), {})())
            if p7.error or p7.needs_review:
                detail = redact(f"P7: {p7.error or p7.warnings or 'needs_review'}", _known_secrets(root))
                return GateCheck("30s P7→P8→P9 媒体链路", "fail", detail)
            p8 = stage_p8(job, cfg, type("Opts", (), {"preview": False})())
            if p8.error or p8.needs_review:
                detail = redact(f"P8: {p8.error or p8.warnings or 'needs_review'}", _known_secrets(root))
                return GateCheck("30s P7→P8→P9 媒体链路", "fail", detail)
            p9 = stage_p9(job, cfg, type("Opts", (), {"export_draft": True})())
            report = json.loads((job / "draft_export_report.json").read_text(encoding="utf-8"))
            final = job / "final" / "output.mp4"
            draft = job / "drafts" / job.name / "draft_content.json"
            ok = p9.error is None and report.get("ok") is True and final.exists() and draft.exists()
            detail = f"P7/P8/P9 ok; final={final.exists()} draft={draft.exists()}"
            return GateCheck("30s P7→P8→P9 媒体链路", "pass" if ok else "fail", detail)
    except Exception as exc:  # noqa: BLE001
        return GateCheck("30s P7→P8→P9 媒体链路", "fail", redact(f"gate exception: {exc}", _known_secrets(root)))


def _fal_key(root: Path) -> str | None:
    value = os.environ.get("FAL_KEY")
    if value:
        return value
    try:
        from dotenv import dotenv_values

        return dotenv_values(root / ".env").get("FAL_KEY")
    except Exception:  # noqa: BLE001
        return None


def check_edge_tts(root: Path) -> GateCheck:
    async def synthesize(path: Path) -> None:
        import edge_tts

        await edge_tts.Communicate("发布级语音 smoke 测试。", "zh-CN-XiaoxiaoNeural").save(str(path))

    with tempfile.TemporaryDirectory(prefix="doc2video-tts-") as temp:
        path = Path(temp) / "tts.mp3"
        try:
            asyncio.run(synthesize(path))
        except Exception as exc:  # noqa: BLE001
            return GateCheck("live:edge-tts", "fail", redact(str(exc), _known_secrets(root)))
        return GateCheck("live:edge-tts", "pass" if path.exists() and path.stat().st_size else "fail",
                         f"bytes={path.stat().st_size if path.exists() else 0}")


def check_fal(root: Path) -> GateCheck:
    key = _fal_key(root)
    if not key:
        return GateCheck("live:FAL 直连 smoke", "blocked", "FAL_KEY 缺失;未伪造通过")
    secrets = _known_secrets(root)
    result = run_command("live:FAL 直连 smoke", [sys.executable, "scripts/spikes/fal_smoke.py"], root,
                         timeout_s=600, secrets=secrets)
    report_path = root / "docs" / "spikes" / "fal_smoke.json"
    if result.status == "fail":
        return result
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        smoke = report.get("smoke", {})
        if smoke.get("ok") is True:
            return GateCheck(result.name, "pass", "FAL contract smoke passed")
        return GateCheck(result.name, "fail", redact(f"FAL smoke 未通过: {smoke.get('detail', '')}", secrets))
    except Exception as exc:  # noqa: BLE001
        return GateCheck(result.name, "fail", redact(f"无法读取 FAL smoke 报告: {exc}", secrets))


def live_checks(root: Path) -> list[GateCheck]:
    secrets = _known_secrets(root)
    return [
        run_command("live:ffmpeg Spike", [sys.executable, "scripts/spikes/ffmpeg_verify.py"], root,
                    timeout_s=900, secrets=secrets),
        run_command("live:Ollama Spike", [sys.executable, "scripts/spikes/ollama_bench.py"], root,
                    timeout_s=900, secrets=secrets),
        run_command("live:faster-whisper Spike", [sys.executable, "scripts/spikes/whisper_bench.py"], root,
                    timeout_s=900, secrets=secrets),
        check_edge_tts(root),
        check_fal(root),
    ]


def run_gate(root: Path, media_smoke: bool = False, live: bool = False,
             candidate: Path | None = None) -> dict[str, Any]:
    secrets = _known_secrets(root)
    checks = [
        live_scope_check(live),
        check_schemas(root),
        *check_runtime(root),
        run_command("pytest", [sys.executable, "-m", "pytest", "-q"], root, secrets=secrets),
        run_command("ruff", [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"], root,
                    secrets=secrets),
    ]
    with tempfile.TemporaryDirectory(prefix="doc2video-build-") as temp:
        checks.append(run_command(
            "uv build", ["uv", "build", "--out-dir", temp], root, timeout_s=600, secrets=secrets,
        ))
    if media_smoke or live:
        checks.append(check_media_smoke(root))
    if live:
        checks.extend(live_checks(root))
    return build_report(checks, __version__, candidate_descriptor(candidate))


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    """root 仅供测试注入,不作为 CLI 参数暴露(不构成旁路)。"""
    # 非 UTF-8 终端(重定向/老控制台)下中文输出不得崩溃:降级替换而非抛异常。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="doc2video 发布级验收 gate(无旁路参数)")
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run", help="执行 gate 检查(默认)")
    verify_parser = sub.add_parser("verify", help="发布前校验:候选产物必须已绑定全绿 gate 报告")
    for p in (run_parser, parser):
        p.add_argument("--media-smoke", action="store_true", help="真实运行 30 秒 P7/P8/P9 链路")
        p.add_argument("--live", action="store_true", help="运行外部 Provider/Spike smoke")
        p.add_argument("--report", default="docs/release/release_gate.json")
        p.add_argument("--candidate", default=None,
                       help="候选发布产物路径,报告与其 SHA-256 绑定(供 verify 校验)")
    verify_parser.add_argument("candidate", help="待发布产物路径")
    args = parser.parse_args(argv)
    root = root or Path(__file__).resolve().parents[1]
    if args.command == "verify":
        ok, detail = verify_candidate(root, Path(args.candidate))
        print(f"verify: {'PASS' if ok else 'REJECT'}  {detail}")
        return 0 if ok else 2
    candidate = Path(args.candidate) if args.candidate else None
    try:
        report = run_gate(root, media_smoke=args.media_smoke, live=args.live, candidate=candidate)
    except FileNotFoundError as exc:
        print(f"gate 拒绝: {exc}", file=sys.stderr)
        return 2
    report_path = root / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for check in report["checks"]:
        mark = {"pass": "✓", "warn": "!", "blocked": "⊘", "fail": "✗"}[check["status"]]
        print(f"  {mark} {check['name']}: {check['detail'][-240:]}")
    print(f"\nGate: {report['status']}  release_ready={report['release_ready']}")
    print(f"Report: {report_path}")
    return 0 if report["release_ready"] else 2


if __name__ == "__main__":
    sys.exit(main())
