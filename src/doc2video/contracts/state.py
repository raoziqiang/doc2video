"""Job 状态机契约(快照形态;转移规则在 doc2video.state 实现)。"""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field

from .common import Contract


class StageStatus(str, Enum):
    pending = "pending"
    running = "running"
    committing = "committing"
    succeeded = "succeeded"
    succeeded_with_warnings = "succeeded_with_warnings"
    needs_review = "needs_review"
    failed = "failed"
    cancelled = "cancelled"
    invalidated = "invalidated"


class StageState(Contract):
    status: StageStatus
    fingerprint: str | None = Field(default=None, description="stage_fingerprint(输入+配置+Prompt+模型+Schema+代码+工具)")
    artifact_manifest_ref: str | None = Field(default=None, description="artifact_manifest.json 相对路径")
    attempts: int = Field(ge=0, default=0)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobState(Contract):
    schema_version: Literal["1.0"] = "1.0"
    job_id: str = Field(pattern=r"^\d{8}_[a-f0-9]{6,}$")
    revision: int = Field(ge=0, description="每次保存 +1;0 = 尚未落盘的初始快照")
    updated_at: datetime
    stages: dict[str, StageState] = Field(description="key=P0..P9")
