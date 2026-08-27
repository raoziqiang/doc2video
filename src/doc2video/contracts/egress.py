"""外发审计契约:egress_manifest(P5)+ egress_report(P8 汇总)。"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .common import Contract


class EgressCall(Contract):
    provider: str = Field(min_length=1)
    fields_sent: list[str] = Field(default_factory=list, description="外发的字段清单")
    client_request_uuid: str = Field(min_length=1)
    request_id: str | None = None
    at: datetime
    cost: float | None = None


class EgressManifest(Contract):
    """P5 提交;云 LLM 阶段(P2/P3/P4)的外发以事件写入 events.jsonl,P8 汇总。"""

    schema_version: Literal["1.0"] = "1.0"
    calls: list[EgressCall] = Field(default_factory=list)


class EgressReport(Contract):
    schema_version: Literal["1.0"] = "1.0"
    generated_at: datetime
    calls: list[EgressCall] = Field(default_factory=list)
