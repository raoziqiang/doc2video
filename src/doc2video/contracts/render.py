"""P7 契约:render_manifest(staging 成片 + 命令参数 + 产物哈希)。"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .artifact import ArtifactEntry
from .common import Contract


class RenderManifest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    staging_path: str = Field(min_length=1)
    entries: list[ArtifactEntry] = Field(min_length=1)
    command_argv: list[str] = Field(default_factory=list, description="实际执行的命令参数数组(审计用)")
    bgm_mix_argv: list[str] = Field(default_factory=list, description="BGM 混音命令参数数组(审计用;无 BGM 时为空)")
    fonts: list[str] = Field(default_factory=list, description="字幕使用的字体记录(可审计/可复现, S2.5 L-04)")
    timeline_ref_sha256: str | None = Field(default=None, description="引用的 render_timeline.json 内容哈希")
    committed_at: datetime
