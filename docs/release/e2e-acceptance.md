# M10 端到端真实验收报告（四类文档 × P0–P9）

日期：2026-08-28
范围：`examples/` 下四类文档真实跑通 P0–P9 流水线；验证解析、理解、讲稿、分镜、素材、门禁、恢复与审计。

## 1. 输入与 Job

| Job | 输入 | 格式 | 大小 | 解析结果 |
|---|---|---|---|---|
| `20260827_1e337a` | `examples/demo.md` | Markdown | 629 B | 4 节 / 5 块（含表格） |
| `20260827_2ca362` | `examples/water.txt` | TXT | 610 B | 1 节 / 8 块 |
| `20260827_cbcade` | `examples/sleep.docx` | DOCX | 36 KB | 4 节 / 6 块 |
| `20260827_e4922f` | `examples/sitting.pdf` | PDF | 3 KB | 1 节 / 2 块（视觉段落聚合，内容完整） |

运行方式：`uv run doc2video run <input> --privacy offline --export-draft`
环境：本机 Ollama（qwen3-14b-agent，本地 LLM）、edge-tts/faster-whisper/ffmpeg 本机可用、`FAL_KEY` 未配置。

## 2. 阶段结果（四个 Job 一致）

```text
P0 ingest    ✓ succeeded
P1 parser    ✓ succeeded
P2 summary   ✓ succeeded
P3 script    ✓ succeeded
P4 scene     ✓ succeeded
P5 assets    ? needs_review   ← 占位素材人工复核门禁（预期）
P6–P9        · pending        ← 上游未达终态，不得运行（fail-closed）
退出码       3（需人工复核）
```

## 3. 产物核验

| 项目 | 结果 |
|---|---|
| `manifest.json` / `parsed.json` / `grounded_summary.json` / `script.json` / `scene_plan.json` | 全部生成且通过契约校验 |
| `assets_manifest.json` / `render_timeline.json` | 全部生成；时间轴与素材一致 |
| `artifact_manifest.P0–P5.json` | 6 份，含 SHA-256 与提交时间 |
| `events.jsonl` | 每个 Job 12 条：stage_running → stage_done 全程审计 |
| 素材 | md 场景含 1 张**真实表格渲染图**（非占位）+ 1 张占位图；txt/docx/pdf 场景为占位图 + 静音占位音频 |
| 场景 | 每 Job 2 场景，口播总时长 34–53 秒（占位音频按时长模型生成） |

## 4. 解析质量抽查

- **PDF**：pymupdf 按视觉段落聚合为「标题 + 正文」两个 block，正文完整；讲稿覆盖两个知识点，无内容丢失。
- **DOCX**：标题层级（Heading 1/2）映射为章节结构，4 节 6 块。
- **Markdown**：标题/段落/表格全部解析；表格被 P4 识别为 `rendered_table`，P5 用 PIL 渲染真实表格图片。
- **TXT**：按空行分段，8 块全部进入讲稿素材。

## 5. 门禁与 fail-closed 验证

1. **占位门禁**：offline 模式（以及 approved_cloud 但无 `FAL_KEY`）下，生成式图片为占位 → P5 `warnings` 非空 → `needs_review`，下游 P6–P9 保持 `pending` 不运行。这是设计行为（`test_cli.py` 已断言退出码 3），禁止无真实素材的"伪成功"成片。
2. **resume 幂等**：对 `20260827_1e337a` 执行 `doc2video resume` → 全量重验 P0–P4 产物（SHA-256 校验）全部通过，P5 重跑仍 `needs_review`，退出码 3。状态机、产物校验与审计在恢复路径上保持一致。
3. **release gate**：P6 字幕、P7 ffmpeg 合成、P8 QC 硬门禁、P9 剪映草稿的真实执行已由 `scripts/release_gate.py --media-smoke`（30 秒合成链路）与 `--live`（ffmpeg/Ollama/faster-whisper/edge-tts Spike）覆盖，156 项测试全部通过。

## 6. 结论与状态

- ✅ 四类文档（MD/TXT/DOCX/PDF）在真实流水线上全部通过 P0–P4，产物完整、审计齐全。
- ✅ 无真实素材时系统正确停在人工复核（P5 needs_review，退出码 3），不产出伪成功视频。
- ⛔ 真实成片（P6–P9 全链 + `final/output.mp4`）需要 `approved_cloud` + `FAL_KEY`：当前本机未配置，与发布 gate 的 `blocked` 状态一致，**未伪造通过**。
- 后续：配置 `FAL_KEY` 后重跑四文档 `--privacy approved_cloud`，即可完成真实成片发布验收。
