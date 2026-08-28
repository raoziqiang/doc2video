"""P0 接收与规范化测试:限额、ZIP bomb、扩展名与 magic 不一致、快照不可变。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from doc2video.config import load_config
from doc2video.pipeline.p0_ingest import RejectError, stage_p0
from doc2video.pipeline.stages import StageResult

from . import fixtures as fx


class Opts:
    privacy_mode = "offline"


def cfg_override(**kw) -> dict:
    cfg = copy.deepcopy(load_config())
    for k, v in kw.items():
        cfg["limits"][k] = v
    return cfg


def run_p0(tmp_path: Path, src: Path, cfg=None) -> StageResult:
    job = tmp_path / "20260827_abc123"
    job.mkdir()
    return stage_p0(job, src, cfg or load_config(), Opts())


def test_p0_ingest_snapshot_and_manifest(tmp_path):
    src = fx.make_md(tmp_path)
    result = run_p0(tmp_path, src)
    job = tmp_path / "20260827_abc123"
    assert (job / "input" / src.name).exists()
    assert (job / "manifest.json").exists()
    assert result.artifacts


def test_p0_zip_bomb_rejected(tmp_path):
    src = fx.make_zipbomb_docx(tmp_path, uncompressed_mb=60)
    with pytest.raises(RejectError, match="REJECT_TOO_LARGE"):
        run_p0(tmp_path, src, cfg=cfg_override(max_docx_uncompressed_mb=10))


def test_p0_docx_entry_limit(tmp_path):
    src = fx.make_zipbomb_docx(tmp_path, uncompressed_mb=1, entries=100)
    with pytest.raises(RejectError, match="REJECT_TOO_LARGE"):
        run_p0(tmp_path, src, cfg=cfg_override(max_docx_entries=10))


def test_p0_malformed_rejected(tmp_path):
    src = fx.make_malformed(tmp_path)
    with pytest.raises(RejectError, match="REJECT_MALFORMED"):
        run_p0(tmp_path, src)


def test_p0_unsupported_extension(tmp_path):
    src = tmp_path / "x.exe"
    src.write_bytes(b"MZ....")
    with pytest.raises(RejectError, match="REJECT_UNSUPPORTED"):
        run_p0(tmp_path, src)


def test_p0_extension_magic_mismatch_tolerated(tmp_path):
    """扩展名与 magic 不一致:以 magic 为准(pdf 内容伪装 .txt 仍按 pdf 接收)。"""
    src = tmp_path / "weird.txt"
    src.write_bytes(fx.make_text_pdf(tmp_path).read_bytes())
    result = run_p0(tmp_path, src)
    assert result.artifacts  # 接受(按 magic 判 pdf)


def test_p0_size_limit(tmp_path):
    src = tmp_path / "big.txt"
    src.write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(RejectError, match="REJECT_TOO_LARGE"):
        run_p0(tmp_path, src, cfg=cfg_override(max_input_mb=1))


def test_p0_utf8_boundary_split_not_rejected(tmp_path):
    """嗅探窗口(8192B)正好切断多字节字符 → 合法文本仍须接收,不得误判 unknown。"""
    src = tmp_path / "boundary.md"
    # 8191 个 ASCII + 一个三字节汉字:第 8192 字节恰好落在汉字中间。
    src.write_bytes(b"A" * 8191 + "中文内容".encode() * 100)
    result = run_p0(tmp_path, src)
    assert result.artifacts
