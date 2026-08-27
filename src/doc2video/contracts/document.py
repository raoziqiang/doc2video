"""P1 契约:parsed.json(解析产物,含阅读顺序与坐标)。"""

from typing import Literal

from pydantic import Field

from .common import Contract


class Block(Contract):
    block_id: str = Field(pattern=r"^b\d+$", description="稳定块 ID(构造规则见方案 4.2)")
    type: Literal["paragraph", "list", "table", "image"]
    text: str = ""
    page: int | None = None
    bbox: list[float] | None = None
    reading_order: int | None = None
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)


class Section(Contract):
    id: str = Field(pattern=r"^s\d+$")
    level: int = Field(ge=1, le=6)
    heading: str
    blocks: list[Block] = Field(default_factory=list)


class ParsedMeta(Contract):
    source: str
    type: str
    pages: int = Field(ge=1)
    chars: int = Field(ge=0)
    parser_version: str = Field(min_length=1)


class ParsedDocument(Contract):
    schema_version: Literal["1.0"] = "1.0"
    meta: ParsedMeta
    title: str
    sections: list[Section] = Field(min_length=1)
