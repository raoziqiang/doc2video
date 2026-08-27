"""内容寻址缓存:canonical JSON + SHA-256(方案 4.6.2)。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


class CacheStore:
    """文件型内容寻址缓存。缓存命中必须经过 SHA-256 校验,损坏条目视为 miss。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("缓存 key 必须是 64 位小写 SHA-256")
        return self.root / key[:2] / key[2:]

    def get(self, key: str) -> Path | None:
        path = self._path(key)
        if not path.exists() or not path.is_file():
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != self._file_digest_marker(path):
            # 兼容无 marker 的旧条目:缓存文件名只表达请求 key,不能表达内容 hash;
            # 因此无 marker 条目不可信,删除后 miss。
            path.unlink(missing_ok=True)
            return None
        return path

    def _file_digest_marker(self, path: Path) -> str:
        marker = path.with_suffix(path.suffix + ".sha256")
        if not marker.exists():
            return ""
        return marker.read_text(encoding="ascii").strip()

    def put(self, key: str, source: str | Path) -> Path:
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(source)
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        target.with_suffix(target.suffix + ".sha256").write_text(digest, encoding="ascii")
        return target



def canonical_json(obj: Any) -> str:
    """影响输出的全部参数 → 稳定字节序 JSON(键排序、紧凑分隔)。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_key(*parts: str) -> str:
    """缓存键 = SHA-256(有序部件拼接)。"""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
