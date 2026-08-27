"""Provider 注册与构建。测试通过 monkeypatch doc2video.providers.build_llm 注入假实现。"""

from __future__ import annotations

from typing import Any

from .base import LLMError, LLMProvider, repair_json_syntax
from .llm_ollama import OllamaLLM

__all__ = ["LLMError", "LLMProvider", "OllamaLLM", "build_llm", "repair_json_syntax"]


def build_llm(cfg: dict[str, Any]) -> LLMProvider:
    provider = cfg["llm"]["provider"]
    if provider == "ollama":
        return OllamaLLM(cfg)
    raise LLMError(f"未注册的 LLM provider: {provider}")
