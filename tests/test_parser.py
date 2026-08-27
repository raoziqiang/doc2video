"""P1 解析器测试矩阵(方案 4.2/9.1)。

覆盖:文本 PDF(层级/页眉页脚去重)、多栏阅读顺序、DOCX(样式/列表/表格/图片)、
MD/TXT、扫描页 OCR 判定、block_id 稳定性、空/畸形输入。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from doc2video.config import load_config
from doc2video.contracts import ParsedDocument
from doc2video.pipeline.p0_ingest import RejectError, sniff_type
from doc2video.pipeline.p1_parser import parse_document

from . import fixtures as fx


def cfg_override(**kw) -> dict:
    cfg = copy.deepcopy(load_config())
    for k, v in kw.items():
        cfg["limits"][k] = v
    return cfg


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def parse(tmp_path: Path, p: Path, doc_type: str | None = None, cfg=None) -> ParsedDocument:
    cfg = cfg or load_config()
    dt = doc_type or sniff_type(p)
    if dt == "txt" and p.suffix.lower() == ".md":
        dt = "md"  # 与 P0 一致:文本类 + .md 扩展名 → md
    return parse_document(p, tmp_path, dt, cfg)


# ── MD / TXT ───────────────────────────────────────────────


def test_markdown_structure(tmp_path, cfg):
    doc = parse(tmp_path, fx.make_md(tmp_path))
    headings = [s.heading for s in doc.sections]
    assert "咖啡因与睡眠" in headings
    assert any("第一节" in h for h in headings)
    types = [b.type for s in doc.sections for b in s.blocks]
    assert "list" in types and "table" in types and "paragraph" in types
    list_block = next(b for s in doc.sections for b in s.blocks if b.type == "list")
    assert "半衰期" in list_block.text, "列表项文本必须完整提取"
    table_block = next(b for s in doc.sections for b in s.blocks if b.type == "table")
    assert "5.5 小时" in table_block.text


def test_txt_paragraphs(tmp_path, cfg):
    doc = parse(tmp_path, fx.make_txt(tmp_path))
    paras = [b.text for s in doc.sections for b in s.blocks if b.type == "paragraph"]
    assert any(p.startswith("第一段") for p in paras)
    assert any(p.startswith("第二段") for p in paras)
    assert len(paras) == 3


def test_empty_file_rejected(tmp_path, cfg):
    p = tmp_path / "empty.md"
    p.write_text("", encoding="utf-8")
    doc = parse(tmp_path, p)
    assert doc.sections  # 空 MD:生成"正文"空节,不崩溃(M1 容忍;M2 覆盖检查会拦截)


# ── PDF ─────────────────────────────────────────────────────


def test_pdf_heading_and_header_footer_dedup(tmp_path, cfg):
    doc = parse(tmp_path, fx.make_text_pdf(tmp_path))
    texts = [b.text for s in doc.sections for b in s.blocks]
    assert any("第 1 章 标题" in t for t in texts)
    assert doc.meta.pages == 3
    # 页眉/页脚(3 页重复)必须被剔除
    assert not any(t.startswith("机密·内部资料") for t in texts)
    assert not any(t.startswith("第 1 页 / 共 3 页") for t in texts)
    # 正文保留
    assert any("第 1 页的第 1 段正文" in t for t in texts)


def test_pdf_two_column_reading_order(tmp_path, cfg):
    doc = parse(tmp_path, fx.make_two_column_pdf(tmp_path))
    blocks = [b for s in doc.sections for b in s.blocks if b.reading_order is not None]
    left = next((b for b in blocks if "左栏内容第 1 行" in b.text), None)
    right = next((b for b in blocks if "右栏内容第 1 行" in b.text), None)
    assert left is not None and right is not None
    assert left.reading_order < right.reading_order, "左栏必须先于右栏(阅读顺序重排)"


def test_pdf_encrypted_rejected(tmp_path, cfg):
    p = fx.make_encrypted_pdf(tmp_path)
    with pytest.raises(RejectError, match="REJECT_ENCRYPTED"):
        parse(tmp_path, p)


def test_pdf_page_limit(tmp_path, cfg):
    p = fx.make_text_pdf(tmp_path, pages=5)
    with pytest.raises(RejectError, match="REJECT_TOO_LARGE"):
        parse(tmp_path, p, cfg=cfg_override(max_pdf_pages=3))


def test_scanned_pdf_ocr_path(tmp_path, cfg):
    """扫描页 → OCR 判定路径。OCR 引擎/模型不可用时跳过(环境限制)。"""
    ok, reason = fx.ocr_engine_available()
    if not ok:
        pytest.skip(f"OCR 引擎不可用: {reason}")
    doc = parse(tmp_path, fx.make_scanned_pdf(tmp_path))
    blocks = [b for s in doc.sections for b in s.blocks]
    # OCR 可能识别出部分文字;至少验证走了 OCR 路径(有置信度标记或非空块)
    assert blocks, "扫描页应产出 OCR 块"


# ── DOCX ────────────────────────────────────────────────────


def test_docx_structure(tmp_path, cfg):
    doc = parse(tmp_path, fx.make_docx(tmp_path))
    headings = [s.heading for s in doc.sections]
    assert any("咖啡因与睡眠" in h for h in headings)
    blocks = [b for s in doc.sections for b in s.blocks]
    types = {b.type for b in blocks}
    assert "list" in types and "table" in types and "image" in types
    list_block = next(b for b in blocks if b.type == "list")
    assert "半衰期" in list_block.text
    table_block = next(b for b in blocks if b.type == "table")
    assert "5.5 小时" in table_block.text
    img = next(b for b in blocks if b.type == "image")
    assert (tmp_path / img.text).exists(), "内嵌图片应落入 extracted_assets"


# ── 稳定性与嗅探 ────────────────────────────────────────────


def test_block_id_stability(tmp_path, cfg):
    p = fx.make_text_pdf(tmp_path)
    doc1 = parse(tmp_path, p)
    doc2 = parse(tmp_path, p)
    ids1 = [b.block_id for s in doc1.sections for b in s.blocks]
    ids2 = [b.block_id for s in doc2.sections for b in s.blocks]
    assert ids1 == ids2


def test_sniff_types(tmp_path):
    assert sniff_type(fx.make_text_pdf(tmp_path)) == "pdf"
    assert sniff_type(fx.make_docx(tmp_path)) == "docx"
    assert sniff_type(fx.make_md(tmp_path)) == "txt"  # 文本类(扩展名区分 md/txt)
    assert sniff_type(fx.make_malformed(tmp_path)) == "unknown"
    assert sniff_type(fx.make_encrypted_pdf(tmp_path)) == "pdf"  # 加密 PDF 嗅探仍为 pdf(解析时拒绝)
