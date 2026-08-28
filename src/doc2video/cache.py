"""内容寻址缓存:canonical JSON + SHA-256(方案 4.6.2)。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
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
        existing = self.get(key)
        if existing is not None:
            return existing  # 共享缓存可能被多作业并发写:已有合法条目不重写
        # 先写临时文件再 os.replace:并发读者永远看不到半成品内容。
        tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        shutil.copy2(source, tmp)
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        os.replace(tmp, target)
        target.with_suffix(target.suffix + ".sha256").write_text(digest, encoding="ascii")
        return target



def garbage_collect(
    root: str | Path,
    max_bytes: int | None = None,
    ttl_days: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """S3.4 缓存 GC:先按 TTL 淘汰,再按 LRU(最旧优先)压到体积上限内。

    只删数据文件与其 .sha256 marker;空子目录顺手清理。缓存损坏视为可弃(与 get 的语义一致)。
    """
    root = Path(root)
    removed = 0
    freed = 0
    if not root.is_dir():
        return {"removed": removed, "freed_bytes": freed, "kept_bytes": 0, "dry_run": dry_run}
    entries: list[tuple[float, int, Path]] = []
    for data in root.rglob("*"):
        if data.is_file() and not data.name.endswith(".sha256") and not data.name.startswith("."):
            entries.append((data.stat().st_mtime, data.stat().st_size, data))

    def _remove(path: Path) -> None:
        nonlocal removed, freed
        size = path.stat().st_size
        if not dry_run:
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".sha256").unlink(missing_ok=True)
        removed += 1
        freed += size

    now = time.time()
    if ttl_days is not None:
        cutoff = now - ttl_days * 86400
        kept = []
        for mtime, _size, path in entries:
            if mtime < cutoff:
                _remove(path)
            else:
                kept.append((mtime, _size, path))
        entries = kept
    if max_bytes is not None:
        total = sum(size for _m, size, _p in entries)
        for _mtime, _size, path in sorted(entries):  # 最旧优先淘汰,直到回到上限内
            if total <= max_bytes:
                break
            size = path.stat().st_size if path.exists() else 0
            _remove(path)
            total -= size
    kept_bytes = 0
    for data in root.rglob("*"):
        if data.is_file() and not data.name.endswith(".sha256"):
            kept_bytes += data.stat().st_size
        if data.is_dir() and not dry_run and not any(data.iterdir()):
            data.rmdir()
    return {"removed": removed, "freed_bytes": freed, "kept_bytes": kept_bytes, "dry_run": dry_run}


def canonical_json(obj: Any) -> str:
    """影响输出的全部参数 → 稳定字节序 JSON(键排序、紧凑分隔)。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_key(*parts: str) -> str:
    """缓存键 = SHA-256(有序部件拼接)。"""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
