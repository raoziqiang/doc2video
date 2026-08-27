"""从 Pydantic 契约生成 Draft 2020-12 JSON Schema 到 schemas/ 目录。

用法: python -m doc2video.contracts.generate_schemas
CI 防漂移: tests/test_contracts.py::test_schemas_no_drift
"""

import json
from pathlib import Path

from . import manifest, document, summary, script, scene_plan, assets, subtitles, render, qc, egress, artifact, state, draft
from .common import to_schema

# 契约名 → 模型类(提交到仓库的 Schema 全集)
REGISTRY: dict[str, type] = {
    "manifest": manifest.Manifest,
    "parsed": document.ParsedDocument,
    "grounded_summary": summary.GroundedSummary,
    "script": script.Script,
    "scene_plan": scene_plan.ScenePlan,
    "assets_manifest": assets.AssetsManifest,
    "render_timeline": assets.RenderTimeline,
    "subtitles": subtitles.Subtitles,
    "render_manifest": render.RenderManifest,
    "qc_report": qc.QCReport,
    "release_manifest": qc.ReleaseManifest,
    "egress_manifest": egress.EgressManifest,
    "egress_report": egress.EgressReport,
    "artifact_manifest": artifact.ArtifactManifest,
    "state": state.JobState,
    "draft_export_report": draft.DraftExportReport,
}

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"


def generate_all(out_dir: Path = SCHEMAS_DIR) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, model_cls in REGISTRY.items():
        schema = to_schema(model_cls, name)
        p = out_dir / f"{name}.schema.json"
        p.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written[name] = p
    return written


if __name__ == "__main__":
    for name, path in generate_all().items():
        print(f"{name:22s} -> {path}")
    print(f"\n共 {len(REGISTRY)} 类 Schema 写入 {SCHEMAS_DIR}")
