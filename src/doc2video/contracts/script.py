"""P3 契约:script(讲稿,场景带源引用与 claim→fact 绑定)。"""

from typing import Literal

from pydantic import Field

from .common import Contract


class Claim(Contract):
    fact_id: str = Field(pattern=r"^f\d{2,}$")
    quote: str = Field(min_length=1, description="讲稿中与事实对应的原句片段")


class ScriptScene(Contract):
    id: str = Field(pattern=r"^sc\d{2,3}$")
    chapter: str = Field(min_length=1)
    narration: str = Field(min_length=60, max_length=180, description="口播稿 60–180 字")
    est_duration_s: float = Field(ge=10, le=45, description="估算时长 13–40 秒,取 10–45 硬界")
    source_block_ids: list[str] = Field(min_length=1)
    source_pages: list[int] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)


class Script(Contract):
    schema_version: Literal["1.0"] = "1.0"
    scenes: list[ScriptScene] = Field(min_length=1, max_length=20)
