"""全部契约的合法示例实例(过模型校验与生成 Schema)。"""

from datetime import UTC, datetime

from doc2video.contracts import (
    ArtifactEntry,
    ArtifactManifest,
    AssetsManifest,
    AudioAsset,
    Block,
    ChapterPlanItem,
    ChapterSummary,
    Claim,
    Coverage,
    Cue,
    DraftExportReport,
    EgressManifest,
    EgressReport,
    Fact,
    FactNormalized,
    GroundedSummary,
    ImageAsset,
    JobState,
    Manifest,
    NativeMark,
    ParsedDocument,
    ParsedMeta,
    QCCheck,
    QCReport,
    ReleaseManifest,
    RenderManifest,
    RenderTimeline,
    SceneAssets,
    ScenePlan,
    ScenePlanScene,
    Script,
    ScriptScene,
    Section,
    StageState,
    StageStatus,
    StyleTemplate,
    Subtitles,
    TimelineScene,
)

NARRATION = (
    "大家好,今天我们用五分钟解读这份报告的核心结论,先看它的整体框架与关键数据。"
    "报告指出,过去一年营收增长了百分之二十三,主要驱动力来自海外市场的快速扩张,"
    "同时研发投入占比也创下历史新高。"
)


def ts() -> datetime:
    return datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


def build_examples() -> dict[str, object]:
    ex: dict[str, object] = {}
    ex["manifest"] = Manifest(
        job_id="20260827_ab12cd",
        source="examples/demo.md",
        source_sha256="a" * 64,
        source_size=1024,
        mime_type="text/markdown",
        doc_type="md",
        privacy_mode="offline",
        created_at=ts(),
        config_fingerprint="c" * 64,
        pipeline_version="0.1.0",
    )
    ex["parsed"] = ParsedDocument(
        meta=ParsedMeta(source="demo.md", type="md", pages=1, chars=100, parser_version="0.1.0"),
        title="测试文档",
        sections=[
            Section(
                id="s1",
                level=1,
                heading="第一章",
                blocks=[Block(block_id="b1", type="paragraph", text="内容", page=1, reading_order=1)],
            )
        ],
    )
    ex["grounded_summary"] = GroundedSummary(
        doc_summary="这是一份测试文档的总结。",
        key_points=[],
        chapter_plan=[ChapterPlanItem(chapter="第一章", section_ids=["s1"], planned_scenes=2)],
        chapter_summaries=[
            ChapterSummary(section_ids=["s1"], summary="本章摘要", source_block_ids=["b1"])
        ],
        facts=[
            Fact(
                fact_id="f01",
                kind="number",
                text="营收增长 23%",
                normalized=FactNormalized(value=23, unit="%", polarity="positive"),
                source_block_ids=["b1"],
                source_pages=[1],
            )
        ],
        coverage=Coverage(blocks_seen=1, blocks_total=1),
    )
    ex["script"] = Script(
        scenes=[
            ScriptScene(
                id="sc01",
                chapter="第一章",
                narration=NARRATION,
                est_duration_s=13.3,
                source_block_ids=["b1"],
                source_pages=[1],
                claims=[Claim(fact_id="f01", quote="营收增长 23%")],
            )
        ]
    )
    ex["scene_plan"] = ScenePlan(
        style=StyleTemplate(name="flat-illustration", prefix="扁平插画风格,无文字", negative="text, watermark"),
        scenes=[
            ScenePlanScene(
                id="sc01",
                chapter="第一章",
                narration=NARRATION,
                est_duration_s=13.3,
                visual_desc="演播台上一块大屏幕,屏幕上是报告封面",
                visual_source="generated",
                image_prompt="扁平插画风格,演播台上一块大屏幕,无文字,无水印",
                aspect="16:9",
                source_block_ids=["b1"],
            )
        ],
    )
    ex["assets_manifest"] = AssetsManifest(
        scenes=[
            SceneAssets(
                scene_id="sc01",
                audio=AudioAsset(
                    path="assets/audio/sc01.mp3",
                    duration_s=11.7,
                    native_marks=[NativeMark(text="大家好", start_s=0.5, end_s=1.9)],
                    provider="edge-tts",
                ),
                image=ImageAsset(path="assets/images/ab.png", cache_key="c" * 64, provider="fal"),
            )
        ]
    )
    ex["render_timeline"] = RenderTimeline(
        scenes=[
            TimelineScene(
                id="sc01",
                scene_start_s=0.0,
                lead_s=0.5,
                audio_duration_s=11.7,
                trail_s=0.5,
                scene_total_s=12.7,
                fade_out_start_s=12.4,
            )
        ],
        total_s=12.7,
    )
    ex["subtitles"] = Subtitles(
        cues=[Cue(id="sc01_c1", scene_id="sc01", text="大家好,", start_s=0.6, end_s=1.9, source="native")]
    )
    ex["render_manifest"] = RenderManifest(
        staging_path="staging/output.mp4",
        entries=[ArtifactEntry(path="staging/output.mp4", sha256="a" * 64, size=100, mime="video/mp4")],
        command_argv=["ffmpeg"],
        committed_at=ts(),
    )
    ex["qc_report"] = QCReport(
        status="succeeded",
        checks=[QCCheck(name="可播放性", method="ffprobe", result="pass", detail="ok")],
        summary="通过",
        generated_at=ts(),
    )
    ex["release_manifest"] = ReleaseManifest(
        staging_sha256="a" * 64, final_path="final/output.mp4", linked_at=ts(), qc_status="succeeded"
    )
    ex["egress_manifest"] = EgressManifest(calls=[])
    ex["egress_report"] = EgressReport(generated_at=ts(), calls=[])
    ex["artifact_manifest"] = ArtifactManifest(
        stage="P0",
        revision=1,
        entries=[ArtifactEntry(path="input/demo.md", sha256="a" * 64, size=100, mime="text/markdown")],
        committed_at=ts(),
    )
    ex["state"] = JobState(
        job_id="20260827_ab12cd",
        revision=1,
        updated_at=ts(),
        stages={"P0": StageState(status=StageStatus.succeeded)},
    )
    ex["draft_export_report"] = DraftExportReport(ok=True, draft_path="drafts/x", note="剪映6.x")
    return ex
