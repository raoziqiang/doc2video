"""P6 契约:subtitles(对齐 Cue,全片时间轴,必填)。"""

from typing import Literal

from pydantic import Field, model_validator

from .common import Contract


class Cue(Contract):
    id: str = Field(min_length=1)
    scene_id: str = Field(pattern=r"^sc\d{2,3}$")
    text: str = Field(min_length=1)
    start_s: float = Field(ge=0, description="全片时间轴(由 render_timeline 换算)")
    end_s: float = Field(ge=0)
    source: Literal["native", "aligned", "asr_fallback"] = Field(
        description="native=TTS 原生边界;aligned=强制对齐;asr_fallback=ASR 低置信兜底"
    )

    @model_validator(mode="after")
    def check_range(self) -> "Cue":
        if self.end_s <= self.start_s:
            raise ValueError("cue 要求 end_s > start_s")
        return self


class Subtitles(Contract):
    schema_version: Literal["1.0"] = "1.0"
    cues: list[Cue] = Field(min_length=1)
