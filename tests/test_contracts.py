"""契约测试:示例过模型校验、过生成 Schema、Schema 无漂移、条件校验。"""

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from doc2video.contracts import ScenePlanScene, ScriptScene, StyleTemplate
from doc2video.contracts.generate_schemas import REGISTRY, SCHEMAS_DIR, generate_all

from .examples import NARRATION, build_examples

EXAMPLES = build_examples()


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_schema_file_exists(name: str):
    assert (SCHEMAS_DIR / f"{name}.schema.json").exists(), f"缺少 Schema: {name}"


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_example_validates_against_model(name: str):
    example = EXAMPLES[name]
    model_cls = REGISTRY[name]
    # 转 JSON 再回读,验证序列化闭环
    data = model_cls.model_validate(example).model_dump(mode="json")
    model_cls.model_validate(data)


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_example_validates_against_generated_schema(name: str):
    example = EXAMPLES[name]
    instance = REGISTRY[name].model_validate(example).model_dump(mode="json")
    jsonschema.validate(instance=instance, schema=_load_schema(name))


def test_schemas_no_drift(tmp_path: Path):
    """CI 防漂移:重新生成与已提交 Schema 必须完全一致。"""
    generated = generate_all(tmp_path)
    for name in REGISTRY:
        committed = (SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
        fresh = (generated[name]).read_text(encoding="utf-8")
        assert committed == fresh, f"{name}.schema.json 与 Pydantic 模型漂移 — 请重新生成"


def test_scene_plan_generated_requires_image_prompt():
    base = dict(
        id="sc01", chapter="一", narration=NARRATION, est_duration_s=13.3,
        visual_desc="演播台上一块大屏幕,屏幕上是报告封面",
        visual_source="generated", source_block_ids=["b1"],
    )
    with pytest.raises(ValidationError):
        ScenePlanScene(**base)  # 缺 image_prompt


def test_scene_plan_page_crop_requires_extracted_ref():
    base = dict(
        id="sc01", chapter="一", narration=NARRATION, est_duration_s=13.3,
        visual_desc="原页上半部分图表区域",
        visual_source="page_crop", source_block_ids=["b1"],
    )
    with pytest.raises(ValidationError):
        ScenePlanScene(**base)  # 缺 extracted_ref


def test_narration_bounds():
    too_short = NARRATION[:50]
    with pytest.raises(ValidationError):
        ScriptScene(id="sc01", chapter="一", narration=too_short, est_duration_s=13.3,
                    source_block_ids=["b1"])
    too_long = "长" * 200
    with pytest.raises(ValidationError):
        ScriptScene(id="sc01", chapter="一", narration=too_long, est_duration_s=13.3,
                    source_block_ids=["b1"])


def test_scene_plan_style_enum():
    with pytest.raises(ValidationError):
        StyleTemplate(name="not-a-style", prefix="x", negative="y")
