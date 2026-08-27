"""流水线编排:状态机驱动、原子提交、Job 锁、events 审计、resume 全量重验。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contracts import ArtifactEntry, ArtifactManifest, JobState, StageState, StageStatus
from ..state import (
    TERMINAL_OK,
    EventLog,
    JobLock,
    StateStore,
    atomic_write_text,
    make_fingerprint,
    require_transition,
    sha256_file,
    utcnow,
    verify_artifact_manifest,
)
from .stages import STAGES, StageResult, stage_handler

ARTIFACT_MANIFEST = "artifact_manifest.json"


def new_state(job_id: str) -> JobState:
    return JobState(
        job_id=job_id,
        revision=0,
        updated_at=utcnow(),
        stages={s: StageState(status=StageStatus.pending) for s in STAGES},
    )


def _commit_artifacts(job_dir: Path, stage: str, artifacts: list[tuple[str, str]]) -> str:
    """计算产物哈希并原子提交 artifact_manifest.<stage>.json(每阶段独立文件,唯一可见提交点)。"""
    entries = []
    for rel, mime in artifacts:
        p = job_dir / rel
        entries.append(
            ArtifactEntry(path=rel, sha256=sha256_file(p), size=p.stat().st_size, mime=mime)
        )
    manifest = ArtifactManifest(
        stage=stage, revision=1, entries=entries, committed_at=utcnow()
    )
    ref = f"artifact_manifest.{stage}.json"
    atomic_write_text(job_dir / ref, manifest.model_dump_json(indent=2) + "\n")
    return ref


def _stage_fingerprint(stage: str, cfg: dict[str, Any], pipeline_version: str) -> str:
    """M0 桩级指纹:阶段名 + 配置指纹 + 代码版本。M1 起各阶段补充输入哈希/模型/Prompt。"""
    from ..config import config_fingerprint

    return make_fingerprint(stage, config_fingerprint(cfg), pipeline_version)


def _upstream_ok(state: JobState, stage: str) -> bool:
    idx = STAGES.index(stage)
    return idx == 0 or all(
        state.stages[s].status in TERMINAL_OK for s in STAGES[:idx]
    )


def run_stages(job_dir: Path, cfg: dict[str, Any], opts: Any, source: Path | None = None) -> JobState:
    """顺序执行 STAGES:未达终态的阶段逐个运行;每阶段持 Job 锁 + 原子提交 + 事件审计。

    上游未达终态(succeeded / succeeded_with_warnings)时下游不得运行(方案 6.3)。
    """
    from .. import __version__

    job_dir = Path(job_dir)
    store = StateStore(job_dir)
    events = EventLog(job_dir / "events.jsonl")
    state = store.load() or new_state(job_dir.name)
    if state.revision == 0:
        store.save(state)  # 初始快照落盘

    for stage in STAGES:
        with JobLock(job_dir):
            # 锁内重新加载(防双 resume 并发)
            state = store.load() or new_state(job_dir.name)
            st = state.stages.setdefault(stage, StageState(status=StageStatus.pending))
            if st.status in TERMINAL_OK:
                continue
            if not _upstream_ok(state, stage):
                break  # 上游失败/未完成 → 下游保持 pending,不得运行
            if st.status == StageStatus.invalidated:
                # 级联失效后恢复运行:invalidated → pending → running
                require_transition(st.status, StageStatus.pending)
                st.status = StageStatus.pending
            require_transition(st.status, StageStatus.running)
            st.status = StageStatus.running
            st.attempts += 1
            st.started_at = utcnow()
            st.error = None
            store.save(state)
            events.append("stage_running", {"stage": stage, "attempts": st.attempts})

            try:
                handler = stage_handler(stage)
                kwargs = {"job_dir": job_dir, "cfg": cfg, "opts": opts}
                if stage == "P0":
                    kwargs["source"] = source
                else:
                    kwargs["stage"] = stage
                result: StageResult = handler(**kwargs)  # type: ignore[arg-type]
                if result.error:
                    raise RuntimeError(result.error)

                require_transition(st.status, StageStatus.committing)
                st.status = StageStatus.committing
                store.save(state)

                ref = _commit_artifacts(job_dir, stage, result.artifacts)
                if result.needs_review:
                    final_status = StageStatus.needs_review
                elif result.warnings:
                    final_status = StageStatus.succeeded_with_warnings
                else:
                    final_status = StageStatus.succeeded
                require_transition(st.status, final_status)
                st.status = final_status
                st.artifact_manifest_ref = ref
                st.fingerprint = _stage_fingerprint(stage, cfg, __version__)
                st.finished_at = utcnow()
                store.save(state)
                events.append(
                    "stage_done",
                    {"stage": stage, "status": st.status.value, "warnings": result.warnings},
                )
            except Exception as exc:  # noqa: BLE001 —— 阶段失败关停,记录后继续走终态
                st.status = StageStatus.failed
                st.error = str(exc)
                st.finished_at = utcnow()
                store.save(state)
                events.append("stage_failed", {"stage": stage, "error": str(exc)})
    return state


def verify_and_invalidate(job_dir: Path, state: JobState) -> JobState:
    """resume 全量重验:逐阶段复核 artifact_manifest 产物,损坏 → 从最早脏节点级联失效。"""
    for stage in STAGES:
        st = state.stages.get(stage)
        if st is None or st.status not in TERMINAL_OK:
            continue
        dirty = False
        ref = st.artifact_manifest_ref
        if not ref or not (job_dir / ref).exists():
            dirty = True
        else:
            manifest = ArtifactManifest.model_validate_json(
                (job_dir / ref).read_text(encoding="utf-8")
            )
            if verify_artifact_manifest(job_dir, manifest):
                dirty = True
        if dirty:
            _invalidate_from(state, stage)
            break
    return state


def _invalidate_from(state: JobState, stage: str) -> None:
    idx = STAGES.index(stage)
    for s in STAGES[idx:]:
        st = state.stages.get(s)
        if st and st.status in TERMINAL_OK:
            require_transition(st.status, StageStatus.invalidated)
            st.status = StageStatus.invalidated
