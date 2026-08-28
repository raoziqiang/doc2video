"""S3.4 运维配额:缓存 GC 行为 + `cache status/gc` CLI 接线 + 启动前磁盘预检。"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections import namedtuple

from doc2video.cache import CacheStore, garbage_collect
from doc2video.cli import main


def _key(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _put(store: CacheStore, name: str, data: bytes) -> None:
    src = store.root.parent / f"{name}.src"
    src.write_bytes(data)
    store.put(_key(name), src)
    src.unlink()


# ── garbage_collect 行为 ────────────────────────────────────


def test_gc_lru_evicts_oldest_first(tmp_path):
    store = CacheStore(tmp_path / "cache")
    _put(store, "old", b"A" * 100)
    os.utime(store.get(_key("old")), (1000, 1000))
    _put(store, "new", b"B" * 100)
    stats = garbage_collect(store.root, max_bytes=100)
    assert stats["removed"] == 1
    assert store.get(_key("old")) is None, "最旧条目必须被淘汰"
    assert store.get(_key("new")) is not None, "最新条目必须保留"
    assert stats["kept_bytes"] == 100


def test_gc_ttl_evicts_stale_and_keeps_marker_consistency(tmp_path):
    store = CacheStore(tmp_path / "cache")
    _put(store, "stale", b"x" * 64)
    path = store.get(_key("stale"))
    os.utime(path, (1000, 1000))
    stats = garbage_collect(store.root, ttl_days=1)
    assert stats["removed"] == 1
    assert not path.exists()
    assert not path.with_suffix(path.suffix + ".sha256").exists(), "marker 必须随数据一起删除"


def test_gc_dry_run_changes_nothing(tmp_path):
    store = CacheStore(tmp_path / "cache")
    _put(store, "a", b"1" * 50)
    os.utime(store.get(_key("a")), (1000, 1000))
    stats = garbage_collect(store.root, ttl_days=1, dry_run=True)
    assert stats["removed"] == 1
    assert store.get(_key("a")) is not None, "dry-run 不得实际删除"


def test_gc_missing_root_is_noop(tmp_path):
    stats = garbage_collect(tmp_path / "nope", max_bytes=1)
    assert stats == {"removed": 0, "freed_bytes": 0, "kept_bytes": 0, "dry_run": False}


# ── CLI 接线 ────────────────────────────────────────────────


def test_cli_cache_gc_and_status(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws"
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(ws))
    store = CacheStore(ws / ".cache" / "assets")
    _put(store, "stale", b"z" * 32)
    os.utime(store.get(_key("stale")), (1000, 1000))

    assert main(["cache", "gc", "--ttl-days", "1"]) == 0
    assert store.get(_key("stale")) is None

    capsys.readouterr()
    assert main(["cache", "status"]) == 0
    out = capsys.readouterr().out
    assert "缓存" in out and "磁盘剩余" in out


def test_cli_cache_gc_dry_run_flag(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(ws))
    store = CacheStore(ws / ".cache" / "assets")
    _put(store, "stale", b"z" * 32)
    os.utime(store.get(_key("stale")), (1000, 1000))
    assert main(["cache", "gc", "--ttl-days", "1", "--dry-run"]) == 0
    assert store.get(_key("stale")) is not None


# ── run 启动前磁盘预检 ──────────────────────────────────────


def test_run_rejects_when_disk_below_minimum(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws"
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(ws))
    doc = tmp_path / "demo.md"
    doc.write_text("# 标题\n正文。\n", encoding="utf-8")
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(total=10, used=10, free=0))

    code = main(["run", str(doc), "--privacy", "offline"])
    assert code == 2, "磁盘低于下限必须硬拒绝"
    assert "磁盘剩余" in capsys.readouterr().err
    assert not ws.exists(), "拒绝后不得留下半成品作业目录"


class _NoLLM:
    """快速失败的 LLM 桩:验证预检未拦截后续阶段即可。"""

    name = "stub"

    def complete_json(self, *a, **k):
        raise RuntimeError("no llm")

    def complete_text(self, *a, **k):
        raise RuntimeError("no llm")

    def count_tokens(self, texts, allow_network=True):
        return [0] * len(texts)


def test_run_passes_when_disk_sufficient(tmp_path, monkeypatch):
    """磁盘充足时预检不拦截:作业目录正常建立(后续阶段失败不影响预检结论)。"""
    ws = tmp_path / "ws"
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(ws))
    doc = tmp_path / "demo.md"
    doc.write_text("# 标题\n正文。\n", encoding="utf-8")
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(total=10, used=1, free=100 * 1024**3))
    from doc2video import providers

    monkeypatch.setattr(providers, "build_llm", lambda cfg: _NoLLM())
    code = main(["run", str(doc), "--privacy", "offline"])
    assert code == 2, "LLM 桩导致的阶段失败应为硬失败退出码 2"
    assert ws.exists() and any(ws.iterdir()), "预检放行后作业目录应已建立"
