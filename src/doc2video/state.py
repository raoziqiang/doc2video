"""崩溃安全状态机与原子提交原语(方案 6.3)。

- 原子提交:tmp → flush → fsync → os.replace
- Job 跨进程排他锁(Windows msvcrt / POSIX fcntl)
- events.jsonl 追加式审计日志(序号 + 校验和 + fsync,读取时截断尾部半行)
- 状态机合法转移表
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import ArtifactManifest, JobState, StageStatus


def utcnow() -> datetime:
    return datetime.now(UTC)


def _json_default(o: Any) -> str:
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not JSON serializable: {type(o)!r}")


# ── 原子提交 ──────────────────────────────────────────────────────


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    _atomic_write(path, data)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    _atomic_write(path, text.encode(encoding))


def atomic_write_json(path: Path, obj: Any) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default)
    atomic_write_text(path, text + "\n")


# ── 哈希与指纹 ────────────────────────────────────────────────────


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_fingerprint(*parts: str) -> str:
    """stage_fingerprint:有序输入哈希 + 配置 + Prompt + 模型 + Schema + 代码 + 工具版本。"""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# ── 追加式事件日志 ────────────────────────────────────────────────


class EventLog:
    """events.jsonl:每行 {seq, ts, event, data} + 校验和;写入 flush+fsync。"""

    SEP = "\u001f"

    def __init__(self, path: Path):
        self.path = path

    def append(self, event: str, data: dict[str, Any] | None = None) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        seq = len(self.read()) + 1
        record = {"seq": seq, "ts": utcnow().isoformat(), "event": event, "data": data or {}}
        body = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=_json_default)
        checksum = sha256_bytes(body.encode("utf-8"))[:16]
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(body + self.SEP + checksum + "\n")
            f.flush()
            os.fsync(f.fileno())
        return seq

    def read(self) -> list[dict[str, Any]]:
        """读取全部合法记录;尾部半行/校验失败行丢弃(记录损坏但不多带)。"""
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            if self.SEP not in line:
                continue  # 尾部半行或损坏
            body, checksum = line.rsplit(self.SEP, 1)
            if sha256_bytes(body.encode("utf-8"))[:16] != checksum:
                continue  # 校验失败
            records.append(json.loads(body))
        return records


# ── Job 跨进程排他锁 ──────────────────────────────────────────────


class JobLock:
    """跨进程排他锁;进程异常退出时 OS 自动释放。锁文件写 pid 供诊断。"""

    def __init__(self, job_dir: Path, timeout_s: float = 30.0):
        self.lock_path = Path(job_dir) / ".job.lock"
        self.timeout_s = timeout_s
        self._fd: Any = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self.lock_path, "a+")
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._fd.seek(0)
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._write_owner()
                return
            except OSError:
                if time.monotonic() > deadline:
                    self.release()
                    raise TimeoutError(
                        f"无法获取 Job 锁(可能另一进程正在处理该 Job): {self.lock_path}"
                    )
                time.sleep(0.2)

    def _write_owner(self) -> None:
        self._fd.seek(0)
        self._fd.truncate()
        self._fd.write(f"pid={os.getpid()} acquired={utcnow().isoformat()}\n")
        self._fd.flush()

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fd.seek(0)
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fd.close()
            self._fd = None

    def __enter__(self) -> JobLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> bool:
        self.release()
        return False


# ── 状态机 ────────────────────────────────────────────────────────

TERMINAL_OK = {StageStatus.succeeded, StageStatus.succeeded_with_warnings}

TRANSITIONS: dict[StageStatus, set[StageStatus]] = {
    StageStatus.pending: {StageStatus.running},
    StageStatus.running: {StageStatus.committing, StageStatus.failed, StageStatus.cancelled},
    StageStatus.committing: {
        StageStatus.succeeded,
        StageStatus.succeeded_with_warnings,
        StageStatus.needs_review,
        StageStatus.failed,
        StageStatus.pending,  # S3.2:提交中途崩溃 → 回退重跑,不得卡死或伪成功
    },
    StageStatus.succeeded: {StageStatus.invalidated},
    StageStatus.succeeded_with_warnings: {StageStatus.invalidated},
    StageStatus.needs_review: {StageStatus.pending, StageStatus.invalidated},
    StageStatus.failed: {StageStatus.pending},
    StageStatus.cancelled: {StageStatus.pending},
    StageStatus.invalidated: {StageStatus.pending},
}


class StateError(RuntimeError):
    pass


def can_transition(frm: StageStatus, to: StageStatus) -> bool:
    return frm == to or to in TRANSITIONS.get(frm, set())


def require_transition(frm: StageStatus, to: StageStatus) -> None:
    if not can_transition(frm, to):
        raise StateError(f"非法状态转移: {frm.value} -> {to.value}")


class StateStore:
    """JobState 的加载与原子保存(保存即 revision+1)。"""

    STATE_FILE = "state.json"

    def __init__(self, job_dir: Path):
        self.job_dir = Path(job_dir)

    @property
    def path(self) -> Path:
        return self.job_dir / self.STATE_FILE

    def load(self) -> JobState | None:
        if not self.path.exists():
            return None
        try:
            return JobState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            # S3.2 故障注入:截断/损坏的 state 不得崩溃,也不得被当不存在(防伪成功)。
            raise StateError(f"state.json 损坏或截断,无法安全续跑: {exc}") from exc

    def save(self, state: JobState) -> None:
        state.revision += 1
        state.updated_at = utcnow()
        atomic_write_text(self.path, state.model_dump_json(indent=2) + "\n")


# ── 产物复核 ──────────────────────────────────────────────────────


def verify_artifact_manifest(job_dir: Path, manifest: ArtifactManifest) -> list[str]:
    """逐产物复核存在性/大小/哈希;返回问题列表(空 = 通过)。"""
    problems: list[str] = []
    for entry in manifest.entries:
        p = job_dir / entry.path
        if not p.exists():
            problems.append(f"缺失: {entry.path}")
            continue
        if p.stat().st_size != entry.size:
            problems.append(f"大小不符: {entry.path}")
            continue
        if sha256_file(p) != entry.sha256:
            problems.append(f"哈希不符: {entry.path}")
    return problems
