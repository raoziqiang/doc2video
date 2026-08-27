"""doc2video CLI:run/resume/batch/watch/report/doctor/preview。

退出码: 0 succeeded / 1 可重试失败 / 2 failed(硬失败)/ 3 needs_review
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import env_key, load_config, workspace_dir
from .contracts import JobState, Manifest, StageStatus
from .pipeline import STAGES, run_stages, verify_and_invalidate
from .state import TERMINAL_OK, StateStore

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}


@dataclass
class RunOptions:
    privacy_mode: str = "offline"
    style: str = "flat-illustration"
    aspect: str = "16:9"
    voice: str = "zh-CN-XiaoxiaoNeural"
    max_duration: int = 600
    bgm: str | None = None
    no_burn_subs: bool = False
    export_draft: bool = False
    preview: bool = False
    llm: str = "local"
    jobs: int = 1


def exit_code_for(state: JobState) -> int:
    statuses = [s.status for s in state.stages.values()]
    if any(s == StageStatus.failed for s in statuses):
        return 2
    if any(s == StageStatus.needs_review for s in statuses):
        return 3
    if all(s in TERMINAL_OK for s in statuses):
        return 0
    return 1


def _print_state(state: JobState) -> None:
    for stage in STAGES:
        st = state.stages.get(stage)
        if st is None:
            continue
        mark = {
            StageStatus.succeeded: "✓",
            StageStatus.succeeded_with_warnings: "✓(warn)",
            StageStatus.failed: "✗",
            StageStatus.needs_review: "?",
            StageStatus.pending: "·",
            StageStatus.running: "▶",
            StageStatus.committing: "…",
            StageStatus.invalidated: "↻",
            StageStatus.cancelled: "⊘",
        }[st.status]
        extra = f"  ({st.error})" if st.error else ""
        print(f"  {mark} {stage}  {st.status.value}{extra}")


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    source = Path(args.input)
    if not source.exists() or not source.is_file():
        print(f"错误: 输入文件不存在: {source}", file=sys.stderr)
        return 2
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        print(f"错误: 不支持的文件类型 {suffix}(支持 {sorted(SUPPORTED_SUFFIXES)})", file=sys.stderr)
        return 2
    limit_mb = cfg["limits"]["max_input_mb"]
    if source.stat().st_size > limit_mb * 1024 * 1024:
        print(f"错误: 文件超过 {limit_mb}MB 上限", file=sys.stderr)
        return 2

    opts = RunOptions(
        privacy_mode=args.privacy or cfg["privacy"]["default_mode"],
        style=args.style,
        aspect=args.aspect,
        voice=args.voice,
        max_duration=args.max_duration,
        bgm=args.bgm,
        no_burn_subs=args.no_burn_subs,
        export_draft=args.export_draft,
        preview=args.preview,
        llm=args.llm,
        jobs=args.jobs,
    )

    ws = workspace_dir(cfg)
    job_id = f"{datetime.now().astimezone():%Y%m%d}_{uuid.uuid4().hex[:6]}"
    job_dir = ws / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    print(f"Job {job_id}  ← {source}")
    print(f"privacy_mode={opts.privacy_mode}  preview={opts.preview}")
    state = run_stages(job_dir, cfg, opts, source=source)
    _print_state(state)
    code = exit_code_for(state)
    print(f"退出码 {code}(0 成功 / 1 可重试 / 2 失败 / 3 需人工)")
    return code


def cmd_resume(args: argparse.Namespace) -> int:
    cfg = load_config()
    ws = workspace_dir(cfg)
    job_dir = ws / args.job_id
    if not job_dir.exists():
        print(f"错误: Job 不存在: {job_dir}", file=sys.stderr)
        return 2
    store = StateStore(job_dir)
    state = store.load()
    if state is None:
        print("错误: 该目录没有 state.json", file=sys.stderr)
        return 2
    print(f"resume {args.job_id} — 全量重验已完成的阶段产物…")
    state = verify_and_invalidate(job_dir, state)
    store.save(state)
    source = None
    if state.stages["P0"].status not in TERMINAL_OK:
        manifest = Manifest.model_validate_json((job_dir / "manifest.json").read_text(encoding="utf-8"))
        source = Path(manifest.source)
    opts = RunOptions(privacy_mode=cfg["privacy"]["default_mode"])
    state = run_stages(job_dir, cfg, opts, source=source)
    _print_state(state)
    return exit_code_for(state)


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config()
    job_dir = workspace_dir(cfg) / args.job_id
    store = StateStore(job_dir)
    state = store.load()
    if state is None:
        print(f"错误: {job_dir} 没有 state.json", file=sys.stderr)
        return 2
    print(f"Job {args.job_id}  revision={state.revision}")
    _print_state(state)
    qc = job_dir / "qc_report.json"
    if qc.exists():
        print("--- qc_report ---")
        print(qc.read_text(encoding="utf-8")[:2000])
    return exit_code_for(state)


# ── doctor ────────────────────────────────────────────────────────


def _run_cmd(argv: list[str]) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        return (out.stdout or "") + (out.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return f"<执行失败: {exc}>"


def _doctor_checks(cfg: dict) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    # Python / uv
    checks.append(("python", sys.version_info >= (3, 11), f"{sys.version.split()[0]}"))

    # ffmpeg
    ff_ver = _run_cmd(["ffmpeg", "-version"])
    has_ffmpeg = "ffmpeg version" in ff_ver
    checks.append(("ffmpeg", has_ffmpeg, ff_ver.splitlines()[0] if has_ffmpeg else "未找到 ffmpeg"))
    if has_ffmpeg:
        filters = _run_cmd(["ffmpeg", "-hide_banner", "-filters"])
        encoders = _run_cmd(["ffmpeg", "-hide_banner", "-encoders"])
        need_filters = ["zoompan", "fade", "sidechaincompress", "loudnorm", "subtitles", "ass"]
        need_encoders = ["libx264", "aac"]
        for name in need_filters:
            ok = any(ln.split() and ln.split()[0] != "Filters:" and len(ln.split()) > 1 and ln.split()[1] == name for ln in filters.splitlines())
            checks.append((f"ffmpeg filter:{name}", ok, "可用" if ok else "缺失"))
        for name in need_encoders:
            ok = any(ln.split() and len(ln.split()) > 1 and ln.split()[1] == name for ln in encoders.splitlines())
            checks.append((f"ffmpeg encoder:{name}", ok, "可用" if ok else "缺失"))

    # CJK 字体
    font = Path(r"C:\Windows\Fonts\msyh.ttc")
    checks.append(("CJK 字体(msyh.ttc)", font.exists(), str(font) if font.exists() else "缺失"))

    # Ollama
    import httpx

    try:
        tags = httpx.get("http://localhost:11434/api/tags", timeout=5)
        if tags.status_code == 200:
            models = [m["name"] for m in tags.json().get("models", [])]
            checks.append(("ollama", True, f"在线,模型: {', '.join(models[:6])}"))
            checks.append(("ollama 模型 qwen3:14b 系", any("qwen3" in m and ("14b" in m or "14b" in m) for m in models), "已安装" if any("qwen3" in m for m in models) else "未安装"))
        else:
            checks.append(("ollama", False, f"HTTP {tags.status_code}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("ollama", False, f"不可达: {exc}"))
    import os

    par = os.environ.get("OLLAMA_NUM_PARALLEL")
    checks.append(("OLLAMA_NUM_PARALLEL", True, f"={par}(未设置则 Ollama 默认 1,须以基准/ps 验证)" if par else "未设置(默认 1,待 Spike 验证)"))

    # FAL
    fal_key = env_key("FAL_KEY")
    checks.append(("FAL_KEY(直连)", bool(fal_key), "已配置" if fal_key else "未配置 — 直连 contract smoke 需 key;否则走 Hermes 网关"))

    # ctranslate2 CUDA
    try:
        import ctranslate2

        cuda_types = ctranslate2.get_supported_compute_types("cuda")
        checks.append(("ctranslate2 CUDA", bool(cuda_types), f"compute types: {cuda_types}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("ctranslate2 CUDA", False, f"{exc}"))

    # 磁盘
    ws = workspace_dir(cfg)
    ws.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(ws)
    free_gb = usage.free / 1024**3
    checks.append(("磁盘剩余", free_gb > 2, f"{free_gb:.1f} GB(阈值 2GB)"))

    # workspace 可写
    probe = ws / ".doctor_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(("workspace 可写", True, str(ws)))
    except OSError as exc:
        checks.append(("workspace 可写", False, str(exc)))
    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"doc2video {__version__} doctor — 环境预检")
    failed = 0
    for name, ok, detail in _doctor_checks(cfg):
        mark = "✓" if ok else "✗"
        if not ok:
            failed += 1
        print(f"  {mark} {name:28s} {detail}")
    print(f"\n结果: {len(_doctor_checks(cfg))} 项检查,{failed} 项失败")
    return 2 if failed else 0


def cmd_not_implemented(args: argparse.Namespace) -> int:
    print("该命令在 M5 实现(batch/watch)。", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="doc2video", description="文档 → 视频 自动化流水线")
    p.add_argument("--version", action="version", version=f"doc2video {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="单文件全流程")
    r.add_argument("input")
    r.add_argument("--style", default="flat-illustration")
    r.add_argument("--aspect", choices=["16:9", "9:16"], default="16:9")
    r.add_argument("--voice", default="zh-CN-XiaoxiaoNeural")
    r.add_argument("--max-duration", type=int, default=600)
    r.add_argument("--bgm", default=None)
    r.add_argument("--no-burn-subs", action="store_true")
    r.add_argument("--export-draft", action="store_true")
    r.add_argument("--privacy", choices=["offline", "approved_cloud", "unrestricted"], default=None)
    r.add_argument("--preview", action="store_true")
    r.add_argument("--llm", choices=["local", "cloud"], default="local")
    r.add_argument("--jobs", type=int, default=1)
    r.set_defaults(func=cmd_run)

    sub.add_parser("resume", help="断点续跑").add_argument("job_id")
    sub.add_parser("report", help="质检摘要").add_argument("job_id")
    sub.add_parser("doctor", help="环境预检").set_defaults(func=cmd_doctor)
    sub.add_parser("preview", help="预览模式(允许降级,产物不可晋升)")
    sub.add_parser("batch", help="批量(M5)").add_argument("dir")
    sub.add_parser("watch", help="监听(M5)").add_argument("dir")

    # 默认 func 分配
    for name in ("resume", "report", "preview", "batch", "watch"):
        sub.choices[name].set_defaults(
            func={"resume": cmd_resume, "report": cmd_report, "preview": cmd_not_implemented,
                  "batch": cmd_not_implemented, "watch": cmd_not_implemented}[name]
        )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
