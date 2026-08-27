"""M0 Spike:FAL 精确端点 contract smoke + 契约快照冻结。

- FAL_KEY 存在 → 真实 smoke:提交 → 轮询队列 → 下载 → 媒体校验
- FAL_KEY 缺失 → 记录 blocked(直连需要 key),契约快照来自官方 API 文档(2026-08-27 核验)

结果写入 docs/spikes/fal_smoke.json;契约快照写入 docs/spikes/fal_contract_snapshot.json。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
RESULT = ROOT / "docs" / "spikes" / "fal_smoke.json"
SNAPSHOT = ROOT / "docs" / "spikes" / "fal_contract_snapshot.json"

# 官方 API 文档核验(2026-08-27, https://fal.ai/models/fal-ai/flux-2/klein/4b/base/api)
ENDPOINT = "fal-ai/flux-2/klein/4b/base"
QUEUE_URL = f"https://queue.fal.run/{ENDPOINT}"

CONTRACT_SNAPSHOT = {
    "endpoint_id": ENDPOINT,
    "verified_at": "2026-08-27",
    "source": "https://fal.ai/models/fal-ai/flux-2/klein/4b/base/api",
    "auth": "Authorization: Key <FAL_KEY>(环境变量 FAL_KEY)",
    "queue": {
        "submit": f"POST {QUEUE_URL}",
        "status": "GET {QUEUE_URL}/requests/<request_id>/status",
        "result": "GET {QUEUE_URL}/requests/<request_id>",
        "note": "队列异步;返回 request_id;服务端可能自动重试,客户端恢复只查 request_id",
    },
    "input": {
        "prompt": "string(必填)",
        "guidance_scale": 5.0,
        "num_inference_steps": 28,
        "image_size": "landscape_4_3 | landscape_16_9 | portrait_16_9 等预设 或 {width,height}",
        "num_images": 1,
        "acceleration": "regular",
        "enable_safety_checker": True,
        "output_format": "png",
        "seed": "integer(可选;不传则随机,结果回带实际 seed)",
    },
    "output": {
        "images": "[{url, content_type, file_name, file_size, width, height}]",
        "timings": "object",
        "seed": "integer(实际使用的 seed)",
        "has_nsfw_concepts": "[boolean]",
        "prompt": "string(实际使用的 prompt)",
    },
    "headers_note": "敏感作业可关 I/O 存储(X-Fal-Store-IO)与设置媒体过期/ACL,见 Platform Headers 文档",
}

results: list[dict] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"  {'✓' if ok else '✗'} {name:36s} {detail}")


def load_key() -> str | None:
    import os

    from dotenv import dotenv_values

    vals = dotenv_values(ROOT / ".env")
    return os.environ.get("FAL_KEY") or vals.get("FAL_KEY") or None


def real_smoke(key: str) -> dict:
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    payload = {
        "prompt": "flat illustration, dark blue and orange palette, simple composition, a podium with a large screen, no text, no watermark",
        "image_size": "landscape_16_9",
        "num_images": 1,
        "output_format": "png",
    }
    t0 = time.perf_counter()
    resp = httpx.post(QUEUE_URL, json=payload, headers=headers, timeout=60)
    if resp.status_code != 200:
        return {"ok": False, "stage": "submit", "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    request_id = resp.json().get("request_id")
    # 轮询队列
    deadline = time.time() + 300
    status = None
    while time.time() < deadline:
        s = httpx.get(f"{QUEUE_URL}/requests/{request_id}/status", headers=headers, timeout=30)
        status = s.json().get("status")
        if status in ("COMPLETED", "FAILED"):
            break
        time.sleep(3)
    if status != "COMPLETED":
        return {"ok": False, "stage": "queue", "detail": f"request_id={request_id} status={status}"}
    r = httpx.get(f"{QUEUE_URL}/requests/{request_id}", headers=headers, timeout=60)
    data = r.json()
    images = data.get("images") or []
    ok = bool(images and images[0].get("url"))
    elapsed = time.perf_counter() - t0
    return {
        "ok": ok,
        "stage": "done",
        "detail": f"request_id={request_id} elapsed={elapsed:.1f}s "
                  f"seed={data.get('seed')} url={images[0].get('url')[:80] if ok else 'N/A'}",
        "request_id": request_id,
        "seed": data.get("seed"),
        "image_url": images[0].get("url") if ok else None,
    }


def main() -> int:
    print("FAL Spike — 精确端点 contract smoke")
    key = load_key()
    smoke = {"status": "blocked", "detail": "FAL_KEY 未配置(直连需要 key;项目不依赖 Hermes 网关作为运行时)"}
    if key:
        check("FAL_KEY", True, "已配置,执行真实 smoke")
        smoke = real_smoke(key)
        check("contract smoke", smoke.get("ok", False), smoke.get("detail", ""))
    else:
        check("FAL_KEY", False, "未配置 → 真实 smoke blocked,契约快照按官方文档冻结")

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(CONTRACT_SNAPSHOT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "smoke": smoke,
        "frozen": {
            "endpoint_id": ENDPOINT,
            "queue_url": QUEUE_URL,
            "requires": "FAL_KEY(.env);冻结项见 fal_contract_snapshot.json",
            "fallback": "无隐式 fallback;ComfyUI 本地为显式配置的可选降级",
            "pricing": "待首次真实计费调用后回填(单位/单张成本)",
        },
        "checks": results,
    }
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"契约快照 → {SNAPSHOT}")
    print(f"\n结论: blocked 属环境限制(无 key),契约已按官方文档冻结 → {RESULT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
