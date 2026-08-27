"""P2 契约:grounded_summary(可追溯原文的摘要与事实表)。"""

from typing import Literal

from pydantic import Field

from .common import Contract


class KeyPoint(Contract):
    text: str = Field(min_length=1)
    source_block_ids: list[str] = Field(min_length=1)


class ChapterPlanItem(Contract):
    chapter: str = Field(min_length=1)
    section_ids: list[str] = Field(min_length=1)
    planned_scenes: int = Field(ge=1, le=20)


class ChapterSummary(Contract):
    section_ids: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)
    source_block_ids: list[str] = Field(min_length=1)


class FactNormalized(Contract):
    value: str | float | None = None
    unit: str | None = None
    polarity: Literal["positive", "negative", "neutral"] = "neutral"
    year: int | None = None


class Fact(Contract):
    fact_id: str = Field(pattern=r"^f\d{2,}$")
    kind: Literal["number", "date", "proper_noun", "unit", "negation"]
    text: str = Field(min_length=1, description="原文事实表述,不得改写")
    normalized: FactNormalized = Field(default_factory=FactNormalized)
    source_block_ids: list[str] = Field(min_length=1)
    source_pages: list[int] = Field(default_factory=list)


class Coverage(Contract):
    blocks_seen: int = Field(ge=0)
    blocks_total: int = Field(ge=0)
    uncovered_block_ids: list[str] = Field(default_factory=list)


class GroundedSummary(Contract):
    schema_version: Literal["1.0"] = "1.0"
    doc_summary: str = Field(min_length=1)
    key_points: list[KeyPoint] = Field(default_factory=list)
    chapter_plan: list[ChapterPlanItem] = Field(min_length=1)
    chapter_summaries: list[ChapterSummary] = Field(min_length=1)
    facts: list[Fact] = Field(default_factory=list)
    coverage: Coverage
