"""测试用 FakeLLM:按 prompt 标记返回固定响应。"""

from __future__ import annotations

from typing import Any

from doc2video.contracts import Contract
from doc2video.providers.base import LLMError, LLMProvider


class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self, responses: dict[str, Any] | None = None,
                 token_counts: dict[str, int] | None = None):
        self.responses = responses or {}
        self.token_counts = token_counts or {}
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system, user, model_cls: type[Contract], json_schema=None) -> Contract:
        self.calls.append(("json", user))
        for marker, payload in self.responses.items():
            if marker in user:
                return model_cls.model_validate(payload)
        raise LLMError(f"FakeLLM 无匹配响应: {user[:120]!r}")

    def complete_text(self, system, user, max_tokens=None) -> str:
        self.calls.append(("text", user))
        for marker, payload in self.responses.items():
            if marker in user and isinstance(payload, str):
                return payload
        raise LLMError(f"FakeLLM 无匹配文本响应: {user[:120]!r}")

    def count_tokens(self, texts: list[str]) -> list[int]:
        return [self.token_counts.get(t, max(1, len(t) // 2)) for t in texts]
