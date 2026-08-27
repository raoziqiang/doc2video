"""状态机原语测试:原子提交/指纹/事件日志/Job 锁/转移表/产物复核。"""

from pathlib import Path

import pytest

from doc2video.contracts import ArtifactEntry, ArtifactManifest, JobState, StageState, StageStatus
from doc2video.state import (
    EventLog,
    JobLock,
    StateError,
    StateStore,
    atomic_write_json,
    atomic_write_text,
    can_transition,
    make_fingerprint,
    require_transition,
    sha256_bytes,
    utcnow,
    verify_artifact_manifest,
)


def test_atomic_write_content_and_no_tmp_left(tmp_path: Path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1})
    assert p.exists()
    assert "tmp" not in [f.name for f in tmp_path.iterdir()]


def test_atomic_write_overwrites(tmp_path: Path):
    p = tmp_path / "x.txt"
    atomic_write_text(p, "one")
    atomic_write_text(p, "two")
    assert p.read_text(encoding="utf-8") == "two"


def test_fingerprint_deterministic_and_sensitive():
    a = make_fingerprint("p0", "cfg1", "v1")
    assert a == make_fingerprint("p0", "cfg1", "v1")
    assert a != make_fingerprint("p0", "cfg2", "v1")


def test_event_log_append_read(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("stage_running", {"stage": "P0"})
    log.append("stage_done", {"stage": "P0"})
    records = log.read()
    assert [r["event"] for r in records] == ["stage_running", "stage_done"]
    assert records[1]["seq"] == 2


def test_event_log_drops_partial_tail(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("a")
    with open(log.path, "a", encoding="utf-8") as f:
        f.write('{"seq": 2, "ts": "x", "ev')  # 半行,无换行
    records = log.read()
    assert len(records) == 1
    assert log.append("b") == 2  # seq 继续(不跳号)


def test_event_log_drops_checksum_corruption(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("good")
    with open(log.path, "a", encoding="utf-8") as f:
        f.write('{"seq": 2, "ts": "x", "event": "bad", "data": {}}\u001f0000000000000000\n')
    assert len(log.read()) == 1


def test_job_lock_timeout_on_double_acquire(tmp_path: Path):
    lock1 = JobLock(tmp_path, timeout_s=0.3)
    lock1.acquire()
    try:
        with pytest.raises(TimeoutError):
            JobLock(tmp_path, timeout_s=0.3).acquire()
    finally:
        lock1.release()
    # 释放后可重新获取
    lock2 = JobLock(tmp_path, timeout_s=0.3)
    lock2.acquire()
    lock2.release()


def test_state_machine_legal_transitions():
    assert can_transition(StageStatus.pending, StageStatus.running)
    assert can_transition(StageStatus.running, StageStatus.committing)
    assert can_transition(StageStatus.committing, StageStatus.succeeded)
    assert can_transition(StageStatus.succeeded, StageStatus.invalidated)
    assert can_transition(StageStatus.invalidated, StageStatus.pending)
    assert can_transition(StageStatus.failed, StageStatus.pending)
    assert can_transition(StageStatus.needs_review, StageStatus.pending)
    assert can_transition(StageStatus.succeeded, StageStatus.succeeded)  # 幂等同态


def test_state_machine_illegal_transitions():
    with pytest.raises(StateError):
        require_transition(StageStatus.succeeded, StageStatus.running)
    with pytest.raises(StateError):
        require_transition(StageStatus.failed, StageStatus.succeeded)
    with pytest.raises(StateError):
        require_transition(StageStatus.succeeded, StageStatus.failed)


def test_state_store_save_bumps_revision(tmp_path: Path):
    store = StateStore(tmp_path)
    state = JobState(
        job_id="20260827_ab12cd", revision=0, updated_at=utcnow(),
        stages={"P0": StageState(status=StageStatus.pending)},
    )
    store.save(state)
    assert state.revision == 1
    store.save(state)
    assert state.revision == 2
    loaded = store.load()
    assert loaded is not None and loaded.revision == 2


def test_verify_artifact_manifest(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    good = ArtifactManifest(
        stage="P0", revision=1,
        entries=[ArtifactEntry(path="a.txt", sha256=sha256_bytes(b"hello"), size=5, mime="text/plain")],
        committed_at=utcnow(),
    )
    assert verify_artifact_manifest(tmp_path, good) == []

    missing = ArtifactManifest(
        stage="P0", revision=1,
        entries=[ArtifactEntry(path="nope.txt", sha256="a" * 64, size=5, mime="text/plain")],
        committed_at=utcnow(),
    )
    assert verify_artifact_manifest(tmp_path, missing)

    bad_hash = ArtifactManifest(
        stage="P0", revision=1,
        entries=[ArtifactEntry(path="a.txt", sha256="b" * 64, size=5, mime="text/plain")],
        committed_at=utcnow(),
    )
    assert verify_artifact_manifest(tmp_path, bad_hash)
