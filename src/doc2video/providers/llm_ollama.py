"""Ollama 原生 API Provider(方案 5.1 已知坑:think:false + 空 content 检测)。"""

from __future__ import annotations

import time
from typing import Any

import httpx
from pydantic import ValidationError

from ..contracts import Contract
from .base import LLMError, LLMProvider, repair_json_syntax


class OllamaLLM(LLMProvider):
    name = "ollama"

    def __init__(self, cfg: dict[str, Any]):
        c = cfg["llm"]
        self.base_url = c["base_url"].rstrip("/")
        self.model = c["model"]
        self.num_ctx = c["num_ctx"]
        self.max_output_tokens = c["max_output_tokens"]
        self.temperature = c["temperature"]
        self.backoff_base = cfg["retry"]["backoff_base_s"]
        self.max_attempts = cfg["retry"]["max_attempts_per_call"]
        self._tokenizer_cache = c.get("tokenizer_cache")
        self._client = httpx.Client(timeout=httpx.Timeout(300.0, read=600.0))

    # ── 底层 ──────────────────────────────────────────────

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(f"{self.base_url}{path}", json=payload)
        if resp.status_code != 200:
            raise LLMError(f"Ollama HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _chat(self, messages: list[dict], fmt: dict | None = None) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"num_ctx": self.num_ctx, "temperature": self.temperature, "think": False},
        }
        if fmt is not None:
            payload["format"] = fmt
        data = self._post("/api/chat", payload)
        content = (data.get("message") or {}).get("content") or ""
        return content

    # ── 接口实现 ──────────────────────────────────────────

    def complete_json(
        self,
        system: str,
        user: str,
        model_cls: type[Contract],
        json_schema: dict[str, Any] | None = None,
    ) -> Contract:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        delay = self.backoff_base
        last_err: str = "unknown"
        for attempt in range(self.max_attempts):
            try:
                # 能力协商第一优先:结构化输出(Ollama format=json-schema)
                content = self._chat(messages, fmt=json_schema)
                if not content.strip():
                    last_err = "空 content(思考模式泄漏?)"
                else:
                    parsed = repair_json_syntax(content)
                    if parsed is None:
                        last_err = f"JSON 不可解析: {content[:120]!r}"
                    else:
                        try:
                            return model_cls.model_validate(parsed)
                        except ValidationError as exc:
                            last_err = f"Schema 校验失败: {exc.errors()[:3]}"
            except LLMError as exc:
                last_err = str(exc)
            if attempt < self.max_attempts - 1:
                time.sleep(delay)
                delay *= 2
        raise LLMError(f"结构化输出失败(重试 {self.max_attempts} 次): {last_err}")

    def complete_text(self, system: str, user: str, max_tokens: int | None = None) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        content = self._chat(messages)
        if not content.strip():
            raise LLMError("空 content(文本模式)")
        return content.strip()

    def count_tokens(self, texts: list[str]) -> list[int]:
        """本地 Qwen3 tokenizer 实测计数(不依赖服务端 /api/embed——本机 Ollama 未开 --embeddings)。"""
        from .token_counter import TokenCounter

        counter = TokenCounter({"llm": {"tokenizer_cache": self._tokenizer_cache}})
        counts = counter.counts(texts)
        if counter.mode == "estimate":
            self._estimate_warning = True
        return counts

