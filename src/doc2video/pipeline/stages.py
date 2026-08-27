"""流水线阶段注册。P0/P1 真实实现,P2–P9 空桩(M1+ 逐个替换)。

阶段处理器签名:handler(job_dir, cfg, opts, **kw) -> StageResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def stage_stub(job_dir: Path, cfg: dict[str, Any], opts: Any, stage: str) -> StageResult:
    """M0 空桩:P2–P9 暂无实现,只提交空 artifact_manifest。"""
    return StageResult(artifacts=[])


def stage_handler(stage: str):
    """按阶段返回处理器(P0/P1 真实实现,P2–P9 空桩;懒加载避免循环导入)。"""
    if stage == "P0":
        from .p0_ingest import stage_p0

        return stage_p0
    if stage == "P1":
        from .p1_parser import stage_p1

        return stage_p1
    return stage_stub
