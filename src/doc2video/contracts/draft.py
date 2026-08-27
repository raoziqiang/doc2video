"""P9 契约:draft_export_report(剪映草稿导出结果,不回写 qc_report)。"""

from typing import Literal

from pydantic import Field

from .common import Contract


class DraftExportReport(Contract):
    schema_version: Literal["1.0"] = "1.0"
    ok: bool
    draft_path: str | None = None
    error: str | None = None
    note: str = ""
