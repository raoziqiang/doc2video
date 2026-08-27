"""缓存与 CLI 测试:内容寻址键、空管线跑通、resume 全量重验。"""

import json
from pathlib import Path

import jsonschema
import pytest

from doc2video.cache import canonical_json, content_key
from doc2video.cli import main
from doc2video.contracts import JobState, Manifest, ParsedDocument
from doc2video.contracts.generate_schemas import SCHEMAS_DIR
from doc2video.pipeline import STAGES
from doc2video.state import TERMINAL_OK, StateStore

from .fake_llm import FakeLLM

_FAKE_RESPONSES = {
    "【块列表】": {
        "results": [{"block_id": "b1", "summary": "正文内容摘要。", "facts": [], "low_info": False}]
    },
    "【分块摘要】": {
        "doc_summary": "这是演示文档的总摘要,内容简短,用于测试端到端流水线。",
        "key_points": [],
    },
    "【事实表】": {
        "scenes": [{
            "id": "sc01", "chapter": "正文",
            "narration": "大家好,今天我们用五分钟解读这份报告的核心结论,先看它的整体框架与关键数据。报告指出,咖啡因的半衰期是五到六小时,下午三点的咖啡到晚上九点仍有一半留在体内。",
            "est_duration_s": 13.3, "source_block_ids": ["b1"], "source_pages": [1],
        }]
    },
    "【分镜输入】": {
        "scenes": [{
            "id": "sc01",
            "visual_desc": "演讲者在屏幕前讲解报告要点,会议室环境,蓝橙色扁平插画,无文字。",
            "image_prompt": "报告讲解场景,演讲者与展示屏,蓝橙色扁平插画,无文字",
        }]
    },
}


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """CLI 集成测试不依赖真实 LLM(方案:外部调用一律 mock)。"""
    monkeypatch.setattr("doc2video.providers.build_llm", lambda cfg: FakeLLM(_FAKE_RESPONSES))


def test_canonical_json_key_order_independent():
    assert canonical_json({"b": 1, "a": [2, 1]}) == canonical_json({"a": [2, 1], "b": 1})


def test_content_key_deterministic_and_sensitive():
    a = content_key("p1", "p2")
    assert a == content_key("p1", "p2")
    assert a != content_key("p1", "p3")


def _make_doc(tmp_path: Path) -> Path:
    doc = tmp_path / "demo.md"
    doc.write_text("# 标题\n\n正文内容。", encoding="utf-8")
    return doc


def _latest_job(workspace: Path) -> Path:
    jobs = sorted(p for p in workspace.iterdir() if p.is_dir())
    assert jobs, "run 未生成作业目录"
    return jobs[-1]


def test_run_empty_pipeline(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    doc = _make_doc(tmp_path)
    code = main(["run", str(doc), "--privacy", "offline"])
    assert code == 3  # offline 禁止云生成:占位素材触发人工复核
    job = _latest_job(tmp_path / "ws")
    state: JobState = StateStore(job).load()
    for stage in STAGES[:5]:
        assert state.stages[stage].status in TERMINAL_OK, f"{stage} 未达终态"
    assert state.stages["P5"].status.value == "needs_review"
    for stage in STAGES[6:]:
        assert state.stages[stage].status.value == "pending", f"{stage} 不应越过 P5 门禁"
    manifest = Manifest.model_validate_json((job / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.doc_type == "md"
    assert manifest.privacy_mode == "offline"
    assert (job / "input" / "demo.md").exists()
    assert (job / "events.jsonl").exists()
    # P1 真实实现:parsed.json 存在且通过 Schema 与契约校验
    parsed_path = job / "parsed.json"
    assert parsed_path.exists()
    parsed = ParsedDocument.model_validate_json(parsed_path.read_text(encoding="utf-8"))
    assert parsed.sections and any(b.text for s in parsed.sections for b in s.blocks)
    schema = json.loads(
        (SCHEMAS_DIR / "parsed.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(instance=parsed.model_dump(mode="json"), schema=schema)


def test_resume_is_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    doc = _make_doc(tmp_path)
    assert main(["run", str(doc), "--privacy", "offline"]) == 3
    job = _latest_job(tmp_path / "ws")
    before = StateStore(job).load()
    assert main(["resume", job.name]) == 3
    after = StateStore(job).load()
    assert before.revision < after.revision
    for stage in STAGES[:5]:
        # 已完成阶段不重跑(attempts 不增加)
        assert after.stages[stage].attempts == before.stages[stage].attempts
    assert after.stages["P5"].attempts > before.stages["P5"].attempts
    assert after.stages["P5"].status.value == "needs_review"


def test_resume_detects_corrupted_artifact(tmp_path: Path, monkeypatch):
    """全量重验:删除已提交产物 → resume 发现脏节点 → P0 重跑恢复。"""
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    doc = _make_doc(tmp_path)
    assert main(["run", str(doc), "--privacy", "offline"]) == 3
    job = _latest_job(tmp_path / "ws")
    before = StateStore(job).load()
    (job / "input" / "demo.md").unlink()  # 破坏 P0 已提交产物
    assert main(["resume", job.name]) == 3
    after = StateStore(job).load()
    assert after.stages["P0"].attempts > before.stages["P0"].attempts
    assert (job / "input" / "demo.md").exists()  # 已恢复


def test_unsupported_input_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(tmp_path / "ws"))
    bad = tmp_path / "x.exe"
    bad.write_text("MZ", encoding="utf-8")
    assert main(["run", str(bad)]) == 2


def test_doctor_runs(monkeypatch):
    monkeypatch.setenv("DOC2VIDEO_WORKSPACE", str(Path.home() / "Temp" / "doc2video-doctor-ws"))
    code = main(["doctor"])
    assert code in (0, 2)  # 环境项失败返回 2,但不崩溃
