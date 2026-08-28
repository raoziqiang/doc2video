"""LLM Provider 抽象:能力协商、结构化输出、token 计数(方案 4.3/6.2)。"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from ..contracts import Contract


class LLMError(RuntimeError):
    pass


def redact_response_body(text: str) -> str:
    """S3.3 日志脱敏:HTTP 错误体可能回显外发内容 → 只记录长度,不记录内容。"""
    return f"<响应体 {len(text)} 字符,已脱敏>"


def repair_json_syntax(text: str) -> dict | None:
    """有界语法修复(仅括号/引号/尾逗号/代码围栏)——禁止猜修业务字段(方案 6.2)。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    # 提取首段平衡花括号
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    candidate = t[start:end]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # 尾逗号修复
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


class LLMProvider(ABC):
    """统一 LLM 接口。complete_json 内部做能力协商(结构化输出 → JSON mode → 严格解析)。"""

    name: str = "base"

    @abstractmethod
    def complete_json(
        self,
        system: str,
        user: str,
        model_cls: type[Contract],
        json_schema: dict[str, Any] | None = None,
    ) -> Contract:
        """按 model_cls 校验输出;重试上限后仍不合格 → LLMError(fail closed)。"""

    @abstractmethod
    def complete_text(self, system: str, user: str, max_tokens: int | None = None) -> str:
        ...

    @abstractmethod
    def count_tokens(self, texts: list[str], allow_network: bool = True) -> list[int]:
        """目标模型 tokenizer 实测计数(逐条);offline 作业传 allow_network=False 禁止下载。"""
