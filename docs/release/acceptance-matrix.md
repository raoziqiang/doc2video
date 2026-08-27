# 发布级验收矩阵

本矩阵区分“本地集成通过”和“Provider 真实发布就绪”。只有完整 `--live` gate 的所有检查均为 `pass` 时，才允许报告 `release_ready=true`。

## Gate 矩阵

| 编号 | 门禁 | 验证命令/证据 | 当前状态 | 备注 |
|---|---|---|---|---|
| RG-01 | Pydantic 契约与 Draft 2020-12 Schema 无漂移 | `uv run python scripts/release_gate.py` | PASS | gate 自动重新生成并逐文件比较 |
| RG-02 | 全量单元/集成测试 | `uv run pytest -q` | PASS | 当前 gate 记录 156 passed |
| RG-03 | 静态质量 | `uv run ruff check src tests scripts` | PASS | 无 lint 错误 |
| RG-04 | 可安装打包 | `uv build --out-dir <temporary-dir>` | PASS | sdist 与 wheel 均成功生成 |
| RG-05 | 30 秒真实媒体链路 | `--media-smoke` | PASS | P7→P8→P9，真实 ffmpeg、QC、native 草稿 |
| RG-06 | P8 硬门禁与发布协议 | P8 QC 报告 + `os.path.samefile` | PASS | final/output.mp4 与 render/final.mp4 为同一硬链接 |
| RG-07 | pyJianYingDraft native 草稿 | P9 `DraftFolder.load_template()` | PASS | native duration 与素材路径已复核 |
| RG-08 | edge-tts 真实音色 | `--live` | PASS | 真实生成测试 MP3，15696 bytes |
| RG-09 | faster-whisper 真实路径 | `--live` | PASS | 中文覆盖率 1.00、词级时间戳可用；CUDA 缺少 cublas DLL，实际 CPU int8 |
| RG-10 | Ollama 真实结构化输出 | `--live` | PASS | 结构化输出、非空 content、token 计数和并发 Spike 均通过 |
| RG-11 | FAL 精确 endpoint/队列/下载 | `--live` | BLOCKED | 当前本机未配置 `FAL_KEY`；不把 Hermes 网关结果冒充直连 |
| RG-12 | 完整发布就绪 | `--media-smoke --live` | BLOCKED | 任一 live 检查 blocked/fail 即不可发布 |

## 执行入口

### 本地 gate

```bash
uv run python scripts/release_gate.py --media-smoke
```

该入口验证本地代码、Schema、测试、lint、构建和 30 秒实际媒体链路；没有 `--live` 时固定输出：

```text
status=blocked
release_ready=false
```

这是范围门禁，不代表本地测试失败。

### 完整发布 gate

```bash
uv run python scripts/release_gate.py --media-smoke --live
```

完整 gate 还会执行 ffmpeg、Ollama、faster-whisper、edge-tts 和 FAL 真实 smoke。报告输出到：

```text
docs/release/release_gate.json
```

## 通过标准

必须同时满足：

- RG-01 至 RG-10 为 `pass`；
- RG-11 FAL 直连 smoke 为 `pass`，或经过明确的发布豁免流程（本项目当前不自动豁免）；
- 没有 `fail`、`blocked`；
- `release_ready=true`；
- 成片经过 P8 QC，且 `final/output.mp4` 由硬链接晋升；
- `draft_export_report.json` 不得覆盖或修改 `qc_report.json`；
- 报告与日志中不得出现任何凭据值。

## 当前结论

本地实现链路已经通过：

```text
Schema → tests → lint → build → 30s ffmpeg → P8 QC → native P9 draft
```

完整 Provider 发布资格仍取决于 live gate。FAL 直连缺少 `FAL_KEY` 时，系统必须保持 `BLOCKED`，而不是给出伪绿结果。
