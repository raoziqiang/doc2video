"""S3.2 故障注入矩阵(AC-12):写前/写中/写后终止、JSON 截断、哈希不符、5xx、双锁、缓存损坏。

逐项断言:不产生伪成功、截断产物不被接受、状态损坏显式硬失败而非崩溃。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from doc2video.cache import CacheStore, content_key
from doc2video.cli import main
from doc2video.contracts import StageStatus
from doc2video.state import (
    EventLog,
    JobLock,
    StateError,
    StateStore,
    verify_artifact_manifest,
)

from .fake_llm import FakeLLM
from .test_cli import _FAKE_RESPONSES, _latest_job, _make_doc
from .test_m7_p8 import _rendered_job


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: FakeLLM(_FAKE_RESPONSES))


def _run_offline(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    assert main(["run", str(_make_doc(tmp_path)), "--privacy", "offline"]) == 3
    return _latest_job(tmp_path / "ws")


# ── 写中终止:state.json 截断 ─────────────────────────────────────


def test_state_json_truncation_resume_fails_closed(tmp_path: Path, monkeypatch, capsys):
    """截断的 state.json 不得被当不存在(伪成功),也不得崩溃:显式退出码 2。"""
    job = _run_offline(tmp_path, monkeypatch)
    text = (job / "state.json").read_text(encoding="utf-8")
    (job / "state.json").write_text(text[: len(text) // 2], encoding="utf-8")
    assert main(["resume", job.name]) == 2
    assert "损坏" in capsys.readouterr().err
    assert main(["report", job.name]) == 2  # report 同样不得崩溃


def test_state_store_load_raises_state_error_on_truncation(tmp_path: Path):
    store = StateStore(tmp_path)
    (tmp_path / "state.json").write_text('{"job_id": "x", "rev', encoding="utf-8")
    with pytest.raises(StateError):
        store.load()


# ── 写中终止:提交中途崩溃(阶段卡在 committing)──────────────────


def test_committing_crash_recovers_on_resume(tmp_path: Path, monkeypatch):
    """模拟提交中途崩溃:阶段状态停在 committing → resume 回退重跑,不卡死不崩溃。"""
    job = _run_offline(tmp_path, monkeypatch)
    store = StateStore(job)
    state = store.load()
    state.stages["P4"].status = StageStatus.committing
    store.save(state)
    assert main(["resume", job.name]) == 3
    after = StateStore(job).load()
    assert after.stages["P4"].status.value in {"succeeded", "succeeded_with_warnings", "needs_review"}
    assert after.stages["P4"].attempts > 1


# ── 写后终止:事件日志尾部半行与产物篡改 ─────────────────────────


def test_event_log_tolerates_truncated_tail(tmp_path: Path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("a", {"i": 1})
    log.append("b", {"i": 2})
    with open(tmp_path / "events.jsonl", "a", encoding="utf-8") as f:
        f.write('{"seq": 3, "ts": "2026-08-28", "event": "c", "da')  # 写中终止的半行
    records = log.read()
    assert [r["event"] for r in records] == ["a", "b"], "尾部半行必须丢弃,合法记录不得多带"


def test_tampered_committed_artifact_cascades_and_recovers(tmp_path: Path, monkeypatch):
    """篡改已提交产物 → 全量重验级联失效 → resume 重跑恢复(截断/篡改产物不被接受)。"""
    job = _run_offline(tmp_path, monkeypatch)
    parsed = job / "parsed.json"
    data = json.loads(parsed.read_text(encoding="utf-8"))
    data["title"] = "被篡改的标题"
    parsed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = StateStore(job).load()
    assert main(["resume", job.name]) == 3
    after = StateStore(job).load()
    assert after.stages["P1"].attempts > before.stages["P1"].attempts, "P1 产物被篡改必须级联重跑"
    restored = json.loads(parsed.read_text(encoding="utf-8"))
    assert restored["title"] == "demo", "重跑必须恢复为真实解析结果(md title=文件茎名)"


def test_verify_artifact_manifest_detects_hash_mismatch(tmp_path: Path):
    from doc2video.pipeline.runner import _commit_artifacts

    job = tmp_path / "job"
    job.mkdir()
    (job / "a.json").write_text('{"v": 1}', encoding="utf-8")
    _commit_artifacts(job, "P0", [("a.json", "application/json")])
    (job / "a.json").write_text('{"v": 2}', encoding="utf-8")  # 提交后内容被改(等长 → 直接命中哈希层)
    from doc2video.contracts import ArtifactManifest

    manifest = ArtifactManifest.model_validate_json(
        (job / "artifact_manifest.P0.json").read_text(encoding="utf-8")
    )
    problems = verify_artifact_manifest(job, manifest)
    assert problems and "哈希不符" in problems[0]


# ── Provider 5xx:阶段失败 + 可重试 ───────────────────────────────


def test_llm_5xx_marks_stage_failed_then_recovers(tmp_path: Path, monkeypatch):
    class BrokenLLM(FakeLLM):
        def complete_json(self, *args, **kwargs):
            raise RuntimeError("provider 503 simulated")

    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: BrokenLLM(_FAKE_RESPONSES))
    assert main(["run", str(_make_doc(tmp_path)), "--privacy", "offline"]) == 2
    job = _latest_job(tmp_path / "ws")
    state = StateStore(job).load()
    assert state.stages["P2"].status.value == "failed"
    assert state.stages["P6"].status.value == "pending", "下游不得越过失败阶段"
    # 恢复后重试:已完成的 P0/P1 不重跑,已有提交不重复(伪成功防护)
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: FakeLLM(_FAKE_RESPONSES))
    before_p0 = state.stages["P0"].attempts
    assert main(["resume", job.name]) == 3
    after = StateStore(job).load()
    assert after.stages["P0"].attempts == before_p0
    assert after.stages["P2"].status.value == "succeeded"


# ── 双 resume:跨进程锁互斥 ───────────────────────────────────────


def test_double_resume_second_process_is_rejected(tmp_path: Path):
    job = tmp_path / "job"
    job.mkdir()
    barrier = threading.Event()
    released = threading.Event()

    def holder() -> None:
        with JobLock(job, timeout_s=5.0):
            barrier.set()
            released.wait(timeout=10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert barrier.wait(timeout=5), "持锁线程未就绪"
    contender = JobLock(job, timeout_s=1.0)
    with pytest.raises(TimeoutError):
        contender.acquire()
    released.set()
    t.join(timeout=5)
    with JobLock(job, timeout_s=5.0):  # 释放后可重新获取
        pass


# ── 缓存损坏:视为 miss,不得命中脏条目 ───────────────────────────


def test_cache_corruption_treated_as_miss(tmp_path: Path):
    cache = CacheStore(tmp_path / "cache")
    source = tmp_path / "src.bin"
    source.write_bytes(b"original-bytes")
    key = content_key("k1")
    path = cache.put(key, source)
    assert cache.get(key) == path
    path.write_bytes(b"corrupted-bytes")  # 缓存条目内容被破坏
    assert cache.get(key) is None, "损坏条目必须视为 miss"
    assert not path.exists(), "损坏条目必须被清除,防止后续误读"
    restored = cache.put(key, source)
    assert cache.get(key) == restored


# ── 写前终止:候选产物缺失不得伪成功 ──────────────────────────────


def test_p8_missing_candidate_is_hard_fail(tmp_path: Path):
    from types import SimpleNamespace

    from doc2video.config import load_config
    from doc2video.pipeline.p8_qc import stage_p8

    job = _rendered_job(tmp_path)
    (job / "render" / "final.mp4").unlink()  # P7 产物在 P8 前消失
    result = stage_p8(job, load_config(), SimpleNamespace(preview=False))
    assert result.error, "候选缺失必须硬失败"
    assert not (job / "final" / "output.mp4").exists(), "不得晋升任何伪产物"
