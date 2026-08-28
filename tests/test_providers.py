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


def test_chat_think_toplevel_and_num_predict(monkeypatch):
    """S1.3c(H-03):think 必须为 /api/chat 顶层参数(放 options 内被静默忽略),
    且 max_output_tokens 必须以 num_predict 下发,否则思考输出挤占预算。"""
    from doc2video.config import load_config

    cfg = load_config()
    llm = OllamaLLM(cfg)
    seen: dict = {}

    def fake_post(path, payload):
        seen.update(payload)
        return {"message": {"content": "ok"}}

    monkeypatch.setattr(llm, "_post", fake_post)
    llm.complete_text("s", "u")
    assert seen.get("think") is False, "think 必须是顶层参数"
    assert "think" not in seen.get("options", {}), "think 不得藏在 options 内"
    assert seen["options"]["num_predict"] == cfg["llm"]["max_output_tokens"]


def test_count_tokens(monkeypatch):
    """count_tokens 走本地 tokenizer 计数(服务端 --embeddings 未开启)。"""

    class _StubCounter:
        mode = "tokenizer"

        def __init__(self, cfg):
            pass

        def counts(self, texts, allow_network=True):
            return [42, 7]

    monkeypatch.setattr("doc2video.providers.token_counter.TokenCounter", _StubCounter)
    llm = make_llm(monkeypatch, [])
    assert llm.count_tokens(["a", "b"]) == [42, 7]


# ── S3.3 日志脱敏:错误消息不得回显外发内容 ─────────────────

def test_unparseable_output_is_redacted(monkeypatch):
    """模型回显敏感输入且不可解析 → LLMError 只记长度,不携带原文。"""
    marker = "SECRET_MARKER-身份证-110101199001011234"
    bad = f"前置说明 {marker} 这不是合法 JSON"
    llm = make_llm(monkeypatch, [
        {"message": {"content": bad}},
        {"message": {"content": bad}},
    ])
    with pytest.raises(LLMError) as ei:
        llm.complete_json("s", "u", Script)
    assert marker not in str(ei.value), "错误消息不得回显模型输出原文"
    assert "长度=" in str(ei.value)


def test_http_error_body_is_redacted(monkeypatch):
    """非 200 响应体可能回显请求内容 → 错误只保留状态码与长度。"""
    from doc2video.config import load_config

    marker = "SECRET_MARKER-内部合同编号"
    llm = OllamaLLM(load_config())

    class _Resp:
        status_code = 500
        text = f"upstream echoed: {marker}"

        def json(self):
            raise AssertionError("非 200 不得解析响应体")

    monkeypatch.setattr(llm._client, "post", lambda url, json=None: _Resp())
    with pytest.raises(LLMError) as ei:
        llm.complete_text("s", "u")
    assert marker not in str(ei.value)
    assert "已脱敏" in str(ei.value)
    assert "500" in str(ei.value)


def test_tts_failure_redacts_message(tmp_path, monkeypatch):
    """P5 TTS 异常只保留类型名,不携带外发文本(见 p5_assets.make_audio)。"""
    from doc2video.config import load_config
    from doc2video.pipeline import p5_assets

    class _Boom(Exception):
        pass

    def fake_run(*a, **kw):
        raise _Boom("SECRET_MARKER-旁白原文")

    monkeypatch.setattr(p5_assets.asyncio, "run", fake_run)
    cfg = load_config()
    with pytest.raises(p5_assets.AssetError) as ei:
        p5_assets.make_audio("SECRET_MARKER-旁白原文", tmp_path / "a.mp3",
                             cfg, "approved_cloud", tmp_path)
    msg = str(ei.value)
    assert "SECRET_MARKER" not in msg, "TTS 错误不得携带外发文本"
    assert "_Boom" in msg and "已脱敏" in msg


def test_token_counter_offline_forbids_download(tmp_path, monkeypatch):
    """offline 隐私模式:无本地缓存时不得发起外部下载,直接走估算兜底。"""
    from doc2video.providers.token_counter import TokenCounter

    counter = TokenCounter({"llm": {"tokenizer_cache": str(tmp_path / "tok")}})

    def _forbidden():
        raise AssertionError("offline 模式不得触发外部下载")

    monkeypatch.setattr(counter, "_download", _forbidden)
    text = "离线计数测试"
    assert counter.count(text, allow_network=False) == len(text)
    assert counter.mode == "estimate"
