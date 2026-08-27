"""流水线阶段。M0 交付:P0 真实实现(ingest),P1–P9 空桩 —— 验收"空管线跑通"。

M1 起逐个替换空桩为真实实现(见技术方案 v0.2.1 第 4 章)。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import config_fingerprint
from ..contracts import Manifest
from ..state import atomic_write_text, sha256_file, utcnow

STAGES: list[str] = [f"P{i}" for i in range(10)]

MIME_BY_DOC_TYPE = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown",
    "txt": "text/plain",
}


@dataclass
class StageResult:
    """阶段执行结果。artifacts: [(相对 Job 根路径, mime)],由 runner 计算哈希并写 artifact_manifest。"""

    artifacts: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def _write_manifest(job_dir: Path, manifest: Manifest) -> None:
    atomic_write_text(job_dir / "manifest.json", manifest.model_dump_json(indent=2) + "\n")


def stage_p0(job_dir: Path, source: Path, cfg: dict[str, Any], opts: Any) -> StageResult:
    """P0 接收与规范化:复制不可变快照 + manifest.json。"""
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    target = input_dir / source.name
    shutil.copy2(source, target)  # 不可变快照(后续阶段只读)
    sha = sha256_file(target)
    doc_type = source.suffix.lower().lstrip(".")
    manifest = Manifest(
        job_id=job_dir.name,
        source=str(source),
        source_sha256=sha,
        source_size=target.stat().st_size,
        mime_type=MIME_BY_DOC_TYPE.get(doc_type, "application/octet-stream"),
        doc_type=doc_type,  # type: ignore[arg-type]
        privacy_mode=opts.privacy_mode,
        created_at=utcnow(),
        config_fingerprint=config_fingerprint(cfg),
        pipeline_version=__version__,
    )
    _write_manifest(job_dir, manifest)
    return StageResult(
        artifacts=[
            (f"input/{source.name}", manifest.mime_type),
            ("manifest.json", "application/json"),
        ]
    )


def stage_stub(job_dir: Path, cfg: dict[str, Any], opts: Any, stage: str) -> StageResult:
    """M0 空桩:P1–P9 暂无实现,只提交空 artifact_manifest。"""
    return StageResult(artifacts=[])


def stage_handler(stage: str):
    if stage == "P0":
        return stage_p0
    return stage_stub
