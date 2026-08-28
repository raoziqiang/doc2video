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
    """计算产物哈希并原子提交 artifact_manifest.<stage>.json(每阶段独立文件,唯一可见提交点)。

    L-02:revision 基于已存在清单真实递增,重跑/失效后重提交可追溯。
    """
    entries = []
    for rel, mime in artifacts:
        p = job_dir / rel
        entries.append(
            ArtifactEntry(path=rel, sha256=sha256_file(p), size=p.stat().st_size, mime=mime)
        )
    ref = f"artifact_manifest.{stage}.json"
    ref_path = job_dir / ref
    prev_revision = 0
    if ref_path.exists():
        try:
            prev_revision = ArtifactManifest.model_validate_json(
                ref_path.read_text(encoding="utf-8")
            ).revision
        except ValueError:
            prev_revision = 0  # 损坏的旧清单:从 1 重新计版,不阻断提交
    manifest = ArtifactManifest(
        stage=stage, revision=prev_revision + 1, entries=entries, committed_at=utcnow()
    )
    atomic_write_text(ref_path, manifest.model_dump_json(indent=2) + "\n")
    return ref


def _stage_fingerprint(stage: str, job_dir: Path, cfg: dict[str, Any], pipeline_version: str) -> str:
    """stage_fingerprint(方案 S1.3a, H-01):阶段 + 配置 + 上游输入哈希 + Prompt 哈希 + 模型 + 代码版本。

    任一输入/Prompt/模型/配置/代码变更 → 指纹变化 → resume 重验时级联失效重跑。
    """
    from ..config import config_fingerprint

    return make_fingerprint(
        stage,
        config_fingerprint(cfg),
        pipeline_version,
        _inputs_digest(job_dir, stage),
        _prompts_digest(),
        str(cfg.get("llm", {}).get("model_digest") or cfg.get("llm", {}).get("model") or ""),
    )


def _inputs_digest(job_dir: Path, stage: str) -> str:
    """上游全部已提交产物的 (路径, 哈希) 集合哈希;任一上游产物变化 → 下游失效。"""
    idx = STAGES.index(stage)
    parts: list[str] = []
    for s in STAGES[:idx]:
        ref = job_dir / f"artifact_manifest.{s}.json"
        if not ref.exists():
            continue
        manifest = ArtifactManifest.model_validate_json(ref.read_text(encoding="utf-8"))
        for e in sorted(manifest.entries, key=lambda x: x.path):
            parts.append(f"{s}:{e.path}:{e.sha256}")
    return make_fingerprint(*parts) if parts else ""


def _prompts_digest() -> str:
    """src/doc2video/prompts/ 全部提示词文件的内容哈希;Prompt 变更 → 依赖它的阶段失效。"""
    prompts_dir = Path(__file__).resolve().parents[1] / "prompts"
    parts: list[str] = []
    if prompts_dir.exists():
        for p in sorted(prompts_dir.glob("*.txt")):
            parts.append(f"{p.name}:{sha256_file(p)}")
    return make_fingerprint(*parts) if parts else ""


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
            if st.status in (
                StageStatus.invalidated,
                StageStatus.needs_review,
                StageStatus.failed,
                StageStatus.cancelled,
                StageStatus.committing,  # S3.2:提交中途崩溃→ 回退重跑
            ):
                # 断点续跑/人工修复/取消后恢复/提交中崩溃:先回到 pending,再 pending → running
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
                    # 硬门禁失败也要保留已经生成的 QC/审计报告,再把阶段置为 failed。
                    if result.artifacts:
                        require_transition(st.status, StageStatus.committing)
                        st.status = StageStatus.committing
                        store.save(state)
                        st.artifact_manifest_ref = _commit_artifacts(job_dir, stage, result.artifacts)
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
                st.fingerprint = _stage_fingerprint(stage, job_dir, cfg, __version__)
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


def verify_and_invalidate(job_dir: Path, state: JobState, cfg: dict[str, Any]) -> JobState:
    """resume 全量重验(方案 S1.3a):逐阶段复核产物完整性 + 指纹一致性。

    产物损坏或指纹失配(输入/配置/Prompt/模型/代码任一变化) → 从最早脏节点级联失效。
    """
    from .. import __version__

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
        # 旧作业可能无指纹(升级前跑的) → 只在有记录时比对,不逼迁历史状态。
        if not dirty and st.fingerprint is not None and st.fingerprint != _stage_fingerprint(stage, job_dir, cfg, __version__):
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
