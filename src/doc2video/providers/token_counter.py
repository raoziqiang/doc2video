"""本地 tokenizer 实测计数(Qwen3 系)。

策略:HF 下载 tokenizer.json 缓存 → tokenizers 库编码计数;下载失败 → 保守兜底
(1 字符 ≈ 1 token,对中文为高估 → 分块更小,安全侧),并标记估算模式。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

TOKENIZER_URLS = [
    "https://huggingface.co/Qwen/Qwen3-14B/resolve/main/tokenizer.json",
    "https://hf-mirror.com/Qwen/Qwen3-14B/resolve/main/tokenizer.json",
]

DEFAULT_CACHE = Path.home() / ".cache" / "doc2video" / "tokenizer"


class TokenCounter:
    def __init__(self, cfg: dict[str, Any]):
        cache_dir = Path(cfg["llm"].get("tokenizer_cache") or DEFAULT_CACHE)
        self.cache_dir = cache_dir
        self._tokenizer: Any = None
        self.mode = "unknown"  # tokenizer | estimate

    def ensure(self) -> None:
        if self._tokenizer is not None:
            return
        tok_path = self.cache_dir / "qwen3_tokenizer.json"
        if not tok_path.exists():
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            data = self._download()
            if data is None:
                self.mode = "estimate"
                return
            tok_path.write_bytes(data)
        try:
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(str(tok_path))
            self.mode = "tokenizer"
        except Exception:  # noqa: BLE001
            self.mode = "estimate"

    @staticmethod
    def _download() -> bytes | None:
        for url in TOKENIZER_URLS:
            try:
                resp = httpx.get(url, timeout=60, follow_redirects=True)
                if resp.status_code == 200:
                    return resp.content
            except Exception:  # noqa: BLE001, S112 —— 尝试下一个可用下载源
                continue
        return None

    def count(self, text: str) -> int:
        self.ensure()
        if self.mode == "tokenizer":
            return len(self._tokenizer.encode(text, add_special_tokens=False).tokens)
        return max(1, len(text))  # 保守兜底:中文 1 字 ≈ 1 token(高估,安全侧)

    def counts(self, texts: list[str]) -> list[int]:
        return [self.count(t) for t in texts]
