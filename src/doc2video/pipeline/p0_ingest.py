"""P0 接收与规范化(真实实现):magic 嗅探、资源限额、加密/畸形拒绝、不可变快照。"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import config_fingerprint
from ..contracts import Manifest
from ..state import atomic_write_text, sha256_file, utcnow
from .stages import MIME_BY_DOC_TYPE, StageResult

PDF_MAGIC = b"%PDF"
PK_MAGIC = b"PK\x03\x04"
_SNIFF_BYTES = 8192


class RejectError(ValueError):
    """带 REJECT_* 分类的拒绝错误(阶段失败,退出码 2)。"""


def sniff_type(path: Path) -> str:
    """magic 嗅探 + 内容校验;返回 pdf/docx/md/txt/unknown。"""
    with open(path, "rb") as f:
        head = f.read(_SNIFF_BYTES)
    if head.startswith(PDF_MAGIC):
        return "pdf"
    if head.startswith(PK_MAGIC):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
            if "[Content_Types].xml" in names or "word/document.xml" in names:
                return "docx"
        except (zipfile.BadZipFile, OSError):
            return "unknown"
        return "unknown"
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"
    return "txt"  # 文本类;md 按扩展名区分


def _check_docx_zip(path: Path, cfg: dict[str, Any]) -> None:
    """ZIP bomb 防护:解压总量/条目数上限。"""
    lim = cfg["limits"]
    total = 0
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > lim["max_docx_entries"]:
            raise RejectError(f"REJECT_TOO_LARGE: DOCX 条目数超限({len(infos)})")
        for info in infos:
            total += info.file_size
            if total > lim["max_docx_uncompressed_mb"] * 1024 * 1024:
                raise RejectError("REJECT_TOO_LARGE: DOCX 解压总量超限(疑似 ZIP bomb)")


def stage_p0(job_dir: Path, source: Path, cfg: dict[str, Any], opts: Any) -> StageResult:
    """P0:不可信输入 → 受控快照 + manifest。"""
    if not source.exists() or not source.is_file():
        raise RejectError(f"REJECT_UNSUPPORTED: 文件不存在: {source}")
    size_limit = cfg["limits"]["max_input_mb"] * 1024 * 1024
    if source.stat().st_size > size_limit:
        raise RejectError(f"REJECT_TOO_LARGE: 超过 {cfg['limits']['max_input_mb']}MB")

    doc_type = sniff_type(source)
    if doc_type == "unknown":
        raise RejectError(f"REJECT_MALFORMED: 无法识别的文件类型: {source}")
    if doc_type == "txt" and source.suffix.lower() == ".md":
        doc_type = "md"
    if source.suffix.lower() not in (".pdf", ".docx", ".md", ".txt"):
        raise RejectError(f"REJECT_UNSUPPORTED: 不支持扩展名 {source.suffix}")
    if doc_type == "docx":
        _check_docx_zip(source, cfg)

    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    target = input_dir / source.name
    shutil.copy2(source, target)  # 不可变快照(后续阶段只读)
    sha = sha256_file(target)
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
    atomic_write_text(job_dir / "manifest.json", manifest.model_dump_json(indent=2) + "\n")
    return StageResult(
        artifacts=[
            (f"input/{source.name}", manifest.mime_type),
            ("manifest.json", "application/json"),
        ]
    )
