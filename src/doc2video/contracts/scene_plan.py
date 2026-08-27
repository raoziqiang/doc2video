"""P4 契约:scene_plan(分镜规划,含视觉来源与条件校验)。"""

from typing import Literal

from pydantic import Field, model_validator

from .common import Contract

VisualSource = Literal[
    "generated", "extracted_image", "page_crop", "rendered_table", "template_chart", "placeholder"
]


class StyleTemplate(Contract):
    name: Literal["flat-illustration", "business-minimal", "tech-dark", "realistic-photo"]
    prefix: str = Field(min_length=1)
    negative: str = Field(min_length=1)


class ScenePlanScene(Contract):
    id: str = Field(pattern=r"^sc\d{2,3}$")
    chapter: str = Field(min_length=1)
    narration: str = Field(min_length=60, max_length=180)
    est_duration_s: float = Field(ge=10, le=45)
    visual_desc: str = Field(min_length=10)
    visual_source: VisualSource
    image_prompt: str | None = None
    extracted_ref: str | None = None
    aspect: Literal["16:9", "9:16"] = "16:9"
    source_block_ids: list[str] = Field(min_length=1)
    source_pages: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_conditional(self) -> "ScenePlanScene":
        if self.visual_source == "generated" and not self.image_prompt:
            raise ValueError("visual_source=generated 要求 image_prompt")
        if self.visual_source in ("extracted_image", "page_crop") and not self.extracted_ref:
            raise ValueError(f"visual_source={self.visual_source} 要求 extracted_ref")
        return self


class ScenePlan(Contract):
    schema_version: Literal["1.0"] = "1.0"
    style: StyleTemplate
    scenes: list[ScenePlanScene] = Field(min_length=1, max_length=20)
