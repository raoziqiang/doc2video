"""P0 契约:manifest(作业元信息 + 计划内 egress 约束)。"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .common import Contract

DocType = Literal["pdf", "docx", "md", "txt"]
PrivacyMode = Literal["offline", "approved_cloud", "unrestricted"]


class Manifest(Contract):
    """P0 输出;只记录计划内约束,实际外发见 egress_manifest/egress_report(不可变)。"""

    schema_version: Literal["1.0"] = "1.0"
    job_id: str = Field(pattern=r"^\d{8}_[a-f0-9]{6,}$", description="作业 ID(日期_短哈希)")
    source: str = Field(min_length=1, description="原始文档路径")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_size: int = Field(gt=0)
    mime_type: str = Field(min_length=1)
    doc_type: DocType
    privacy_mode: PrivacyMode
    created_at: datetime
    config_fingerprint: str = Field(min_length=1, description="生效配置的哈希")
    pipeline_version: str = Field(min_length=1)
