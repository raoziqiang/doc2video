"""Provider 层测试:JSON 语法修复、Ollama 空 content/重试/结构化输出。"""

from __future__ import annotations

import pytest

from doc2video.contracts import Script
from doc2video.providers import LLMError, OllamaLLM
from doc2video.providers.base import repair_json_syntax

# ── repair_json_syntax ──────────────────────────────────────


def test_repair_plain_json():
    assert repair_json_syntax('{"a": 1}') == {"a": 1}


def test_repair_code_fence():
    assert repair_json_syntax('```json\n{"a": 1}\n```') == {"a": 1}


def test_repair_trailing_comma():
    assert repair_json_syntax('{"a": [1, 2,],}') == {"a": [1, 2]}


def test_repair_extracts_balanced_object():
    out = repair_json_syntax('前置说明\n{"a": {"b": 1}}后置文字')
    assert out == {"a": {"b": 1}}


def test_repair_rejects_non_json():
    assert repair_json_syntax("这不是 JSON") is None
    assert repair_json_syntax('{"a": 1') is None  # 不平衡


def test_repair_does_not_fix_business_fields():
    """语法修复不等于业务修复:缺失字段不会被凭空补上(交由 schema 校验拒绝)。"""
    out = repair_json_syntax('{"narration": "短"}')
    assert out == {"narration": "短"}


# ── OllamaLLM(monkeypatch _post) ────────────────────────────


def make_llm(monkeypatch, responses: list, max_attempts: int = 2) -> OllamaLLM:
    from doc2video.config import load_config

    cfg = load_config()
    cfg["retry"]["max_attempts_per_call"] = max_attempts
    cfg["retry"]["backoff_base_s"] = 0.01  # 测试加速
    llm = OllamaLLM(cfg)

    def fake_post(path, payload):
        if not responses:
            raise AssertionError("fake 响应耗尽")
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(llm, "_post", fake_post)
    return llm


GOOD_SCENE = {
    "scenes": [{
        "id": "sc01", "chapter": "概述",
        "narration": "大家好,今天我们用五分钟解读这份报告的核心结论,先看它的整体框架与关键数据。"
                     "报告指出,咖啡因的半衰期是五到六小时,下午三点的咖啡到晚上九点仍有一半留在体内。",
        "est_duration_s": 13.3, "source_block_ids": ["b1"], "source_pages": [1],
    }]
}


def test_complete_json_structured_ok(monkeypatch):
    llm = make_llm(monkeypatch, [
        {"message": {"content": '{"scenes": ' + _json(GOOD_SCENE["scenes"]) + "}"}},
    ])
    out = llm.complete_json("s", "u", Script)
    assert isinstance(out, Script) and out.scenes[0].id == "sc01"


def _json(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def test_empty_content_retries_then_fails(monkeypatch):
    """空 content 坑:重试后仍空 → LLMError(fail closed)。"""
    llm = make_llm(monkeypatch, [
        {"message": {"content": ""}},
        {"message": {"content": ""}},
    ])
    with pytest.raises(LLMError, match="空 content"):
        llm.complete_json("s", "u", Script)


def test_schema_invalid_retries_then_fails(monkeypatch):
    llm = make_llm(monkeypatch, [
        {"message": {"content": '{"scenes": [{"id": "sc01"}]}'}},  # 缺字段 → ValidationError
        {"message": {"content": '{"scenes": [{"id": "sc01"}]}'}},
    ])
    with pytest.raises(LLMError, match="结构化输出失败"):
        llm.complete_json("s", "u", Script)


def test_json_repair_rescues_trailing_comma(monkeypatch):
    llm = make_llm(monkeypatch, [
        {"message": {"content": '```json\n{"scenes": [{"id": "sc01", "chapter": "概述", '
                                '"narration": "' + GOOD_SCENE["scenes"][0]["narration"] + '", '
                                '"est_duration_s": 13.3, "source_block_ids": ["b1"],}],}\n```'}},
    ])
    out = llm.complete_json("s", "u", Script)
    assert isinstance(out, Script)


def test_count_tokens(monkeypatch):
    """count_tokens 走本地 tokenizer 计数(服务端 --embeddings 未开启)。"""

    class _StubCounter:
        mode = "tokenizer"

        def __init__(self, cfg):
            pass

        def counts(self, texts):
            return [42, 7]

    monkeypatch.setattr("doc2video.providers.token_counter.TokenCounter", _StubCounter)
    llm = make_llm(monkeypatch, [])
    assert llm.count_tokens(["a", "b"]) == [42, 7]
