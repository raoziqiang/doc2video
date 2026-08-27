"""内容寻址缓存:canonical JSON + SHA-256(方案 4.6.2)。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """影响输出的全部参数 → 稳定字节序 JSON(键排序、紧凑分隔)。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_key(*parts: str) -> str:
    """缓存键 = SHA-256(有序部件拼接)。"""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
