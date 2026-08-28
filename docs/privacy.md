# 隐私可执行化（S3.3）

本文档是隐私约束的**可执行口径**：信任矩阵、授权流程、日志脱敏规则均与代码行为一一对应，
任何变更必须同步更新本文档与对应测试。

## 1. 隐私模式（机器强制，非约定）

| 模式 | 语义 | 强制点 |
|---|---|---|
| `offline` | 零云调用 | P8「外发审计」检查：`egress_manifest.calls` 非空 → 硬失败（`p8_qc.py`）；`--llm cloud` 未实现直接拒绝 |
| `approved_cloud` | 经授权的最小外发 | 每次云调用写入 `egress_manifest.json`（provider/fields_sent/client_request_uuid/request_id）；配额 `limits.max_cloud_calls_per_job` 与成本预算硬上限 |
| `unrestricted` | 不推荐 | 同样记审计，但不做模式级拦截 |

隐私模式以 `manifest.json`（P0 不可变产物）为准：`resume` 时即使 `run_options.json` 与之冲突也以
manifest 为准（见 `cli._load_run_options`）。

## 2. Provider 信任矩阵

| Provider | 用途 | 外发字段（代码事实） | 端点 | 留存/区域 | 风险与缓解 |
|---|---|---|---|---|---|
| Ollama（本地） | LLM（P2/P3/P4） | 文档分块文本、Prompt | `localhost:11434` | 本机，无外发 | 默认链路；`--llm cloud` 未实现 |
| edge-tts | 语音合成（P5） | `text`（旁白）、`voice`（音色名）——见 `_record_egress(..., ["text", "voice"])` | Microsoft Edge TTS 服务 | 微软侧留存政策以微软服务条款为准；区域未承诺 | 敏感作业禁用（见授权流程）；审计记录每次调用 |
| FAL | 图像生成（P5） | `prompt`（视觉描述，不含文档原文）——见 `_record_egress(..., ["prompt"])` | `queue.fal.run` | FAL 侧政策（美国）；按账户留存 | 幂等头 `x-fal-client-request-uuid` + intent 预持久化防重复计费；配额硬上限 |
| faster-whisper（本地） | 对齐兜底（P6） | 无（本地推理） | — | 本机 | 模型权重首次下载需网络；离线环境预置 |
| pyJianYingDraft | 草稿导出（P9） | 无网络调用 | — | 本机 | — |

> 矩阵口径必须与 `egress_manifest.json` 的 `fields_sent` 一致；新增/变更外发字段属于隐私变更，
> 必须更新本表并补齐接线测试。

## 3. 敏感作业授权流程

1. **默认拒绝**：未显式传 `--privacy approved_cloud` 的作业按 `offline` 处理。
2. **授权留痕**：`run` 时 `privacy_mode` 写入不可变 `manifest.json` 与 `run_options.json`；
   `resume` 不可提权（以 manifest 为准并告警）。
3. **审批责任人**（人工环节，代码不代办）：敏感领域（医疗/法律/金融/含个人信息文档）启用
   `approved_cloud` 前，须由负责人确认：外发字段清单（本表第 2 节）→ 供应商条款 → 留存期限，
   并将审批记录随作业归档（作业目录内保留审批说明文件）。
4. **高风险领域发布**：维持人工复核，不做全自动承诺（方案待跟进事项 4）。

## 4. 日志与错误响应脱敏（代码事实）

| 位置 | 规则 |
|---|---|
| `providers/base.py::redact_response_body` | HTTP 错误体只记长度，不记内容 |
| `providers/llm_ollama.py` | 非 200 响应体、不可解析的模型输出一律脱敏后入 `LLMError` |
| `pipeline/p5_assets.py` | FAL 提交错误体脱敏；TTS 失败只保留异常类型，不携带外发文本 |
| `scripts/release_gate.py::redact` | gate 报告对环境变量/.env 中的凭据值全量替换 `[REDACTED]` |
| `EgressCall` | 只记字段名清单（`fields_sent`），永不记录字段值 |

回归测试：`tests/test_providers.py`（LLM 错误不泄漏内容）、`tests/test_release_gate.py`（凭据脱敏）。

## 5. 变更纪律

- 新增 Provider 或外发字段 → 更新第 2 节矩阵 + 补 `fields_sent` 断言测试；
- 任何错误路径携带响应原文 → 视为隐私缺陷，按 S3.2 注入矩阵流程回归。
