# doc2video

文档 → 视频自动化流水线：输入 PDF、DOCX、Markdown 或 TXT，经过解析、内容理解、可追溯讲稿、分镜、素材、字幕、ffmpeg 合成、QC 和可选剪映草稿导出。

项目按 P0–P9 顺序执行，阶段产物写入 Job 目录并通过 `state.json`、`artifact_manifest.<stage>.json` 和 `events.jsonl` 审计。核心链路失败时 fail closed，不发布伪成功成片。

## 当前实现状态

| 阶段 | 功能 | 状态 |
|---|---|---|
| P0 | 接收、magic 嗅探、输入快照、限额 | 已实现 |
| P1 | PDF/DOCX/MD/TXT 解析、混合 OCR、结构与来源块 | 已实现 |
| P2 | 本地 tokenizer 分块、grounded summary、事实表 | 已实现 |
| P3 | 分章讲稿、claim → fact、数字/单位一致性 | 已实现 |
| P4 | 分镜规划、Style Bible、视觉来源分类 | 已实现 |
| P5 | 原文素材/表格渲染、生成式素材、内容寻址缓存、时间轴；edge-tts WordBoundary 原生 marks 生产；提交前成本/配额预算（超限 fail closed）；FAL 提交意图预持久化（崩溃后只查询不重提） | 已实现 |
| P6 | native marks 主路径消费（缺失时 faster-whisper 词级对齐 → 字符比例兜底） | 已实现 |
| P7 | Ken Burns、横竖屏、loudnorm、ASS 字幕烧录 | 已实现 |
| P8 | ffprobe/decode/QC、硬链接发布、release manifest | 已实现 |
| P9 | pyJianYingDraft 原生剪映草稿导出 | 已实现 |

## 环境

- Windows 11（代码保持跨平台方向）
- Python 3.11
- uv
- ffmpeg/ffprobe（当前开发机已验证 8.1.2 full build）
- Ollama（默认本地 Qwen3 14B；P2–P4 使用原生 API）
- Microsoft YaHei：`C:\Windows\Fonts\msyh.ttc`
- `pyJianYingDraft>=0.3.0`
- FAL 直连为可选 Provider；密钥只放 `.env`，不要提交

安装依赖：

```bash
uv sync
```

## 基本运行

```bash
uv run python -m doc2video.cli run examples/demo.md --privacy offline
```

隐私模式：

- `offline`：默认；禁止云端生成，无法取得正式图片/语音时写入占位并进入 `needs_review`。
- `approved_cloud`：仅在明确批准后使用受控云 Provider。
- `unrestricted`：由操作者承担完整外发责任。

常用选项：

```bash
# 申请生成剪映草稿
uv run python -m doc2video.cli run examples/demo.md --privacy approved_cloud --export-draft

# 检查环境
uv run python -m doc2video.cli doctor

# 预览模式:独立目录,产物永不晋升发布
uv run python -m doc2video.cli preview examples/demo.md --privacy offline

# 缓存与磁盘配额维护(S3.4)
uv run python -m doc2video.cli cache status
uv run python -m doc2video.cli cache gc [--dry-run]

# 查看 Job 状态和 QC 报告
uv run python -m doc2video.cli report <job_id>
```

退出码：

```text
0  成功
1  可重试/尚未完成
2  硬失败
3  需要人工复核
```

## Job 产物

成功 Job 的典型目录：

```text
<workspace>/<job_id>/
├── manifest.json
├── parsed.json
├── grounded_summary.json
├── script.json
├── scene_plan.json
├── assets_manifest.json
├── render_timeline.json
├── assets/<scene>_marks.json    # 云模式 edge-tts 原生词级边界（离线占位时无）
├── pending_requests.json        # FAL 提交意图（崩溃恢复核对用）
├── subtitles.json
├── render/
│   ├── staging.mp4
│   ├── final.mp4
│   └── subtitles.ass
├── render_manifest.json
├── qc_report.json
├── release_manifest.json
├── final/output.mp4
├── egress_report.json
├── drafts/<job_id>/             # --export-draft 时生成
│   ├── draft_content.json
│   ├── draft_meta_info.json
│   ├── doc2video_manifest.json
│   └── media/
├── draft_export_report.json
├── artifact_manifest.P0.json … artifact_manifest.P9.json
├── state.json
└── events.jsonl
```

`final/output.mp4` 只有 P8 通过后才会以硬链接方式生成；`staging` 和 P7 产物保留不移动。

## 发布级验收

### 本地可重复 gate

真实执行 Schema、runtime、pytest、Ruff、打包和 30 秒 P7→P8→P9 媒体链路：

```bash
uv run python scripts/release_gate.py --media-smoke
```

没有执行 Provider live smoke 时，报告会明确为：

```text
status=blocked
release_ready=false
```

这是 fail-closed 设计，不是测试失败。

### 完整 Provider gate

```bash
uv run python scripts/release_gate.py --media-smoke --live
```

该命令会额外执行：

- ffmpeg Spike；
- Ollama Spike；
- faster-whisper Spike；
- edge-tts 真实语音 smoke；
- FAL 精确队列/下载 smoke。

报告写入：

```text
docs/release/release_gate.json
```

任何 `fail` 或 `blocked` 都不会产生 `release_ready=true`。FAL 直连缺少 `FAL_KEY` 时只记录 `blocked`，不会使用 Hermes 网关结果冒充本地直连验收。

gate 与候选产物绑定（S3.1）：发布候选必须先通过 gate 并绑定 digest，再由 `verify` 核对，无手工绕过入口：

```bash
uv run python scripts/release_gate.py --media-smoke --candidate <候选产物路径>
uv run python scripts/release_gate.py verify <候选产物路径>
```

`verify` 只在 `docs/release/` 中找到 `release_ready=true` 且 SHA-256 一致的报告时放行；报告存在但非绿同样拒绝。

完整验收矩阵见：

```text
docs/release/acceptance-matrix.md
```

相关工程文档：

- `方案滚动台账.md` / `审查滚动台账.md`：**方案与审查文档的唯一整合入口**（最新在前、标注日期；后续开发按后进先出原则以顶部最新条目为准，每阶段完成后在台账顶部追加新条目同步方案）
- `docs/privacy.md`：隐私模式强制点、Provider 信任矩阵、授权流程、日志脱敏规则（S3.3）
- `docs/perf.md`：性能基准矩阵与运维配额（S3.4）；基准数字的诚实口径见文内说明
- `docs/licenses.md`：分发形态决策矩阵与许可证结论（S3.5，2026-08-28 已决策开源，整体许可 AGPL-3.0）
- `tests/test_fault_injection.py`：故障注入矩阵（S3.2：截断/篡改/5xx/双 resume/缓存损坏等）

## 测试与代码质量

```bash
uv run pytest -q
uv run ruff check src tests scripts
uv build --out-dir <temporary-dir>
```

外部模型单元测试全部使用 FakeLLM/mock；真实 Ollama、faster-whisper、ffmpeg 和 Provider smoke 只在明确的集成/发布 gate 中执行。

## 已知限制

- 默认 `privacy=offline` 不会偷偷调用 FAL 或 edge-tts；占位素材会触发 QC/阶段复核。
- 云模式成本护栏基于**冻结预估单价**（`UNIT_PRICE_USD`，按次/按千字折算），与真实账单可能有偏差；`max_cost_per_job` / `max_cost_per_day` / `max_cloud_calls_per_job` 超限均硬失败。
- resume 会全量重验产物完整性与阶段指纹（输入/配置/Prompt/模型/代码）；任一变化 → 从最早受影响阶段级联重跑。
- 本机 faster-whisper CUDA 路径缺少 `cublas64_12.dll`，当前发布 gate 会记录实际降级到 CPU int8 的结果。
- 剪映草稿由 `pyJianYingDraft` 生成；剪映 7+ 的自动导出控件可能不可用，需要在剪映中打开草稿后人工导出。
- P9 是可选交付通道，失败不会改写 P8 的 `qc_report.json`，也不会阻断已经发布的 MP4。
- 超长文档自动分集属于后续阶段；一期超限时拒绝或要求人工拆分。

## 安全约定

- `.env` 不得提交；`.env.example` 只放模板。
- 日志、manifest 和 release gate 报告不保存 API key、token、密码或连接凭据。
- 外部素材路径必须先快照或位于 Job 根目录；P9 拒绝路径穿越。
- 所有阶段产物不可变；下游写独立产物，不回写上游 QC/事实/讲稿文件。

## 许可证（分发形态：开源）

本项目采用 **AGPL-3.0**（见 `LICENSE`）：整体许可须 ≥ AGPL，由依赖 `pymupdf` 的 AGPL copyleft 决定。

- 字体与音色文件永不随仓库或产物分发：字幕字体仅引用运行机已安装的系统字体（见 `config.subtitle.font`）。
- 第三方依赖许可证事实与分发决策矩阵见 `docs/licenses.md`；运行时服务（edge-tts/FAL/模型权重）的权利确认项在文内标注，需人工确认。
