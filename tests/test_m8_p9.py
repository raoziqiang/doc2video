"""M8/P9 TDD 测试:剪映兼容草稿、微秒时间轴、素材安全引用与非阻断失败。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from doc2video.config import load_config
from doc2video.pipeline.p9_jianying import stage_p9

from .test_m7_p8 import _rendered_job


def test_p9_disabled_writes_explicit_not_requested_report(tmp_path: Path):
    job = _rendered_job(tmp_path)
    result = stage_p9(job, load_config(), SimpleNamespace(export_draft=False))
    assert result.error is None and not result.needs_review
    report = json.loads((job / "draft_export_report.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["draft_path"] is None
    assert "未启用" in report["note"]
    assert (job / "draft_export_report.json").as_posix()


def test_p9_exports_editable_compat_draft_with_timeline(tmp_path: Path):
    job = _rendered_job(tmp_path)
    result = stage_p9(job, load_config(), SimpleNamespace(export_draft=True))
    assert result.error is None and not result.needs_review
    report = json.loads((job / "draft_export_report.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    draft_dir = job / report["draft_path"]
    draft = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    compat = json.loads((draft_dir / "doc2video_manifest.json").read_text(encoding="utf-8"))
    assert compat["format"] == "doc2video-jianying-compat"
    assert compat["version"] == "1.0"
    assert compat["duration_us"] == 2_400_000
    assert draft["duration"] == 2_400_000
    tracks = {track["type"]: track for track in draft["tracks"]}
    assert set(tracks) == {"video", "audio", "text"}
    for track_type in ("video", "audio"):
        for segment in tracks[track_type]["segments"]:
            assert segment["target_timerange"]["start"] >= 0
            assert segment["target_timerange"]["duration"] > 0
    assert tracks["video"]["segments"][0]["target_timerange"] == {
        "start": 0, "duration": 2_400_000,
    }
    assert tracks["audio"]["segments"][0]["target_timerange"] == {
        "start": 200_000, "duration": 2_000_000,
    }
    assert tracks["text"]["segments"][0]["target_timerange"] == {
        "start": 200_000, "duration": 2_000_000,
    }
    for material in draft["materials"]["videos"] + draft["materials"]["audios"]:
        assert Path(material["path"]).exists()
        assert str(draft_dir) in material["path"]


def test_p9_does_not_modify_p8_qc_report(tmp_path: Path):
    job = _rendered_job(tmp_path)
    marker = {"status": "succeeded", "immutable": True}
    (job / "qc_report.json").write_text(json.dumps(marker), encoding="utf-8")
    stage_p9(job, load_config(), SimpleNamespace(export_draft=True))
    assert json.loads((job / "qc_report.json").read_text(encoding="utf-8")) == marker


def test_p9_export_failure_is_non_blocking(tmp_path: Path):
    job = _rendered_job(tmp_path)
    assets = json.loads((job / "assets_manifest.json").read_text(encoding="utf-8"))
    assets["scenes"][0]["image"]["path"] = "assets/missing.png"
    (job / "assets_manifest.json").write_text(json.dumps(assets), encoding="utf-8")
    result = stage_p9(job, load_config(), SimpleNamespace(export_draft=True))
    assert result.error is None
    assert result.warnings
    report = json.loads((job / "draft_export_report.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["error"]
    assert not (job / "drafts" / job.name / "draft_content.json").exists()


def test_p9_rewrites_draft_meta_info_paths(tmp_path: Path):
    """M-06:draft_meta_info.json 与 draft_content.json 同样重写为最终草稿路径。"""
    job = _rendered_job(tmp_path)
    result = stage_p9(job, load_config(), SimpleNamespace(export_draft=True))
    assert result.error is None
    report = json.loads((job / "draft_export_report.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    meta_text = (job / report["draft_path"] / "draft_meta_info.json").read_text(encoding="utf-8")
    assert ".building-" not in meta_text, "临时构建目录名不得残留在 meta 中"
    assert json.loads(meta_text), "重写后仍是合法 JSON"


def test_p9_rejects_asset_escape_without_writing_outside_package(tmp_path: Path):
    job = _rendered_job(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not allowed")
    assets = json.loads((job / "assets_manifest.json").read_text(encoding="utf-8"))
    assets["scenes"][0]["image"]["path"] = "../outside.png"
    (job / "assets_manifest.json").write_text(json.dumps(assets), encoding="utf-8")
    result = stage_p9(job, load_config(), SimpleNamespace(export_draft=True))
    assert result.error is None and result.warnings
    report = json.loads((job / "draft_export_report.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert not (job / "drafts" / job.name / "media" / "outside.png").exists()
