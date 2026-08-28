"""P5 契约:assets_manifest + render_timeline(实测时长时间轴)。"""

from typing import Literal

from pydantic import Field

from .common import Contract


class NativeMark(Contract):
    """TTS 原生时间边界。可空:Provider 可能无此能力。

    契约语义(方案 S1.1,对齐 v02 §3.11):
    - 时间基准为音频内相对秒数(0 = 音频起点),不含 lead 偏移;
      P6 消费时叠加 scene_start_s + lead_s。
    - P5 写入前经 normalize 保证:0 <= start_s < end_s <= duration_s、
      按 start_s 有序、无重叠(越界裁剪、重叠去重)。
    - marks 整体缺失/为空属合法状态 → P6 降级到 whisper/字符比例兜底。
    """

    text: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)


class AudioAsset(Contract):
    path: str = Field(min_length=1)
    duration_s: float = Field(gt=0, description="实测时长")
    native_marks: list[NativeMark] | None = None
    provider: str = Field(min_length=1)
    voice: str | None = None
    request_id: str | None = None


class ImageAsset(Contract):
    path: str = Field(min_length=1)
    cache_key: str = Field(min_length=1, description="内容寻址缓存键 SHA-256")
    provider: str = Field(min_length=1)
    model: str | None = None
    request_id: str | None = None
    width: int | None = None
    height: int | None = None
    seed: int | None = None
    placeholder: bool = False


class SceneAssets(Contract):
    scene_id: str = Field(pattern=r"^sc\d{2,3}$")
    audio: AudioAsset | None = None
    image: ImageAsset | None = None


class AssetsManifest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    scenes: list[SceneAssets] = Field(min_length=1)


class TimelineScene(Contract):
    id: str = Field(pattern=r"^sc\d{2,3}$")
    scene_start_s: float = Field(ge=0, description="全片时间轴上的起点")
    lead_s: float = Field(ge=0)
    audio_duration_s: float = Field(gt=0)
    trail_s: float = Field(ge=0)
    scene_total_s: float = Field(gt=0)
    fade_out_start_s: float = Field(ge=0, description="= scene_start_s + scene_total_s − fade_s")


class RenderTimeline(Contract):
    """P5 末尾提交;P6/P7/P8 只引用本文件,不各自计算。"""

    schema_version: Literal["1.0"] = "1.0"
    scenes: list[TimelineScene] = Field(min_length=1)
    total_s: float = Field(gt=0)
