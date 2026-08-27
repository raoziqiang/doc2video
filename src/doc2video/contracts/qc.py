"""P8 契约:qc_report + release_manifest(门禁与发布)。"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .common import Contract

QCStatus = Literal["succeeded", "succeeded_with_warnings", "needs_review", "failed"]
CheckResult = Literal["pass", "warn", "fail"]


class QCCheck(Contract):
    name: str = Field(min_length=1)
    method: str = Field(min_length=1)
    result: CheckResult
    detail: str = ""
    threshold: str | None = None


class QCReport(Contract):
    schema_version: Literal["1.0"] = "1.0"
    status: QCStatus
    checks: list[QCCheck] = Field(min_length=1)
    summary: str = Field(min_length=1)
    generated_at: datetime


class ReleaseManifest(Contract):
    """P8 门禁通过后提交;final 以硬链接晋升,staging 产物保留。"""

    schema_version: Literal["1.0"] = "1.0"
    staging_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    final_path: str = Field(min_length=1)
    linked_at: datetime
    qc_status: QCStatus
