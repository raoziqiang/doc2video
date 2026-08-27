"""契约公共设施:基类 + JSON Schema 生成。"""

from typing import Any, Type

from pydantic import BaseModel, ConfigDict
from pydantic.json_schema import GenerateJsonSchema

SCHEMA_NS = "https://doc2video.local/schemas"
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


class Contract(BaseModel):
    """所有契约模型基类:禁止多余字段(extra=forbid)。"""

    model_config = ConfigDict(extra="forbid")


class _Draft202012(GenerateJsonSchema):
    schema_dialect = DRAFT_2020_12


def to_schema(model_cls: Type[BaseModel], schema_id: str) -> dict[str, Any]:
    """把 Pydantic 模型转成 Draft 2020-12 JSON Schema(带 $schema/$id)。"""
    schema = model_cls.model_json_schema(schema_generator=_Draft202012)
    schema["$schema"] = DRAFT_2020_12
    schema["$id"] = f"{SCHEMA_NS}/{schema_id}.schema.json"
    return schema
