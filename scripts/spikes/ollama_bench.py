"""M0 Spike:Ollama 基准 —— 结构化输出 / 并发 / 上下文 / 空 content 坑验证。

结果写入 docs/spikes/ollama_bench.json。
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
RESULT = ROOT / "docs" / "spikes" / "ollama_bench.json"
BASE = "http://localhost:11434"

results: list[dict] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"  {'✓' if ok else '✗'} {name:36s} {detail}")


def main() -> int:
    print("Ollama Spike — 本机基准")

    # 1. 版本与模型清单
    ver = httpx.get(f"{BASE}/api/version", timeout=5).json()
    tags = httpx.get(f"{BASE}/api/tags", timeout=5).json()
    models = {m["name"]: m for m in tags.get("models", [])}
    print(f"  ollama {ver.get('version')}  模型: {', '.join(models) or '(无)'}")

    # 2. 候选模型挑选:优先 qwen3:14b,其次 qwen3-14b-*
    candidate = None
    for name, info in models.items():
        if "qwen3" in name and ("14b" in name or "14B" in name):
            candidate = name
            break
    if candidate is None:
        candidate = next((n for n in models if "qwen3" in n), None)
    check("候选模型", candidate is not None, candidate or "无 qwen3 系模型")
    if candidate is None:
        return 1
    digest = models[candidate]["digest"]
    print(f"  使用模型: {candidate}  digest={digest}")

    # 3. 环境变量(并发/上下文)
    for var in ("OLLAMA_NUM_PARALLEL", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_KV_CACHE_TYPE",
                "OLLAMA_FLASH_ATTENTION"):
        val = os.environ.get(var)
        check(f"env {var}", True, f"={val}" if val else "未设置")

    # 4. 结构化输出 + think:false + 空 content 坑验证(原生 /api/chat)
    schema = {
        "type": "object",
        "properties": {"scenes": {
            "type": "array", "items": {
                "type": "object",
                "properties": {"id": {"type": "string"},
                               "narration": {"type": "string"}},
                "required": ["id", "narration"],
            },
        }},
        "required": ["scenes"],
    }
    payload = {
        "model": candidate,
        "messages": [{"role": "user",
                      "content": "用 JSON 输出 2 个讲稿场景:每个含 id(sc01/sc02)和 narration(一句中文口播,各 60 字左右)。只输出 JSON。"}],
        "stream": False,
        "format": schema,
        "options": {"num_ctx": 8192, "temperature": 0.3, "think": False},
    }
    t0 = time.perf_counter()
    try:
        resp = httpx.post(f"{BASE}/api/chat", json=payload, timeout=180)
        t1 = time.perf_counter()
        data = resp.json()
        content = data.get("message", {}).get("content") or ""
        latency = t1 - t0
        parsed = json.loads(content) if content.strip().startswith("{") else None
        check("结构化输出", parsed is not None and "scenes" in parsed,
              f"latency={latency:.1f}s 场景数={len(parsed.get('scenes', [])) if parsed else 'N/A'}")
        check("空 content 坑", bool(content.strip()),
              f"content 长度={len(content)}")
        eval_count = data.get("eval_count")
        check("生成 token 数", eval_count and eval_count > 0, f"eval_count={eval_count}")
    except Exception as exc:  # noqa: BLE001
        check("结构化输出", False, f"异常: {exc}")

    # 5. 并发探测:2 个并发请求的墙钟时间(串行化 ⇒ 服务端并行=1)
    def one_req(i: int) -> tuple[int, float]:
        t0 = time.perf_counter()
        r = httpx.post(f"{BASE}/api/chat", json={
            "model": candidate,
            "messages": [{"role": "user", "content": "用一句话回答:1+1=?"}],
            "stream": False,
            "options": {"num_ctx": 8192, "think": False},
        }, timeout=180)
        return i, time.perf_counter() - t0

    with ThreadPoolExecutor(2) as ex:
        times = [t for _, t in ex.map(one_req, range(2))]
    single = sum(times) / 2
    overlap = max(times) < sum(times) * 0.85  # 并行时总耗时远小于串行之和
    check("服务端并发(2 并发请求)", overlap,
          f"耗时 {times[0]:.1f}s / {times[1]:.1f}s(并行={overlap})")

    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "ollama_version": ver.get("version"),
        "models": {n: {"digest": m["digest"],
                       "size": m.get("size"),
                       "context_length": m.get("details", {}).get("context_length")} for n, m in models.items()},
        "frozen": {
            "model": candidate,
            "digest": digest,
            "num_ctx": 8192,
            "think": False,
            "structured_output": "format=json-schema(原生 /api/chat,实测可用)",
            "temperature": 0.3,
            "num_parallel_env": os.environ.get("OLLAMA_NUM_PARALLEL", "unset"),
        },
        "checks": results,
    }
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failed = [c for c in results if not c["ok"]]
    print(f"\n结论: {len(results)-len(failed)}/{len(results)} 通过 → {RESULT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
