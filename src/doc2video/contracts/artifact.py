"""artifact_manifest:多文件阶段的唯一可见提交点。"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .common import Contract


class ArtifactEntry(Contract):
    path: str = Field(min_length=1, description="相对 Job 根的路径")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0)
    mime: str = Field(min_length=1)


class ArtifactManifest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    stage: str = Field(pattern=r"^P\d$")
    revision: int = Field(ge=1)
    entries: list[ArtifactEntry] = Field(default_factory=list)
    committed_at: datetime
